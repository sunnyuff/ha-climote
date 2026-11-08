import asyncio
import json
import pytest
import aiohttp
from unittest.mock import AsyncMock, MagicMock, patch

# Add custom_components to path for testing
import sys
import os
from unittest.mock import MagicMock

# Mock homeassistant modules to prevent import errors in local environments
sys.modules['homeassistant'] = MagicMock()
sys.modules['homeassistant.config_entries'] = MagicMock()
sys.modules['homeassistant.const'] = MagicMock()
sys.modules['homeassistant.core'] = MagicMock()
sys.modules['homeassistant.exceptions'] = MagicMock()
sys.modules['homeassistant.helpers'] = MagicMock()
sys.modules['homeassistant.helpers.update_coordinator'] = MagicMock()
sys.modules['homeassistant.helpers.entity_platform'] = MagicMock()
sys.modules['homeassistant.helpers.aiohttp_client'] = MagicMock()
sys.modules['homeassistant.helpers.config_validation'] = MagicMock()
sys.modules['homeassistant.components'] = MagicMock()
sys.modules['homeassistant.components.climate'] = MagicMock()
sys.modules['homeassistant.components.switch'] = MagicMock()
sys.modules['homeassistant.components.sensor'] = MagicMock()

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))

from custom_components.climote.api import (
    ClimoteAPI,
    ClimoteAuthError,
    ClimoteConnectionError,
)
from custom_components.climote.const import BASE_URL

def make_mock_response(status, text_val, headers=None):
    """Helper to construct a perfect async context manager mock response."""
    mock_res = MagicMock()
    mock_res.status = status
    mock_res.headers = headers or {}
    mock_res.text = AsyncMock(return_value=text_val)
    
    # Handle parsing of json double escaped strings
    async def mock_json():
        return json.loads(text_val)
    mock_res.json = mock_json

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_res)
    return mock_ctx

@pytest.mark.asyncio
async def test_pre_login_security_check_pin_not_required():
    """Test login flow when PIN check returns 0 (no PIN required)."""
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password123")

    mock_index_html = """
    <html>
      <input type="hidden" name="cs_token_rf" value="csrf12345" />
      <script>
        xml.open('GET', '/manager/get-heating-schedule?heatingScheduleId=99999&startday=sunday');
      </script>
    </html>
    """

    mock_xml_data = """<?xml version="1.0" encoding="UTF-8"?>
    <data>
      <zoneInfo>
        <zone id="1"><label>Parlour</label><active>1</active></zone>
        <zone id="2"><label>Attic</label><active>1</active></zone>
        <zone id="3"><label>Tank</label><active>1</active></zone>
      </zoneInfo>
    </data>
    """

    # Sequence of request responses using helper
    session.request.side_effect = [
        make_mock_response(200, "0"),                 # POST security-check
        make_mock_response(302, "", {"Location": "/manager/index"}),  # POST login redirect
        make_mock_response(200, mock_index_html),     # GET index
        make_mock_response(200, mock_xml_data),       # GET XML schedule
    ]

    success = await api.login()

    assert success is True
    assert api._csrf_token == "csrf12345"
    assert api.schedule_id == "99999"
    assert api.zone_labels == {1: "Parlour", 2: "Attic", 3: "Tank"}

@pytest.mark.asyncio
async def test_login_invalid_auth():
    """Test login behavior when Climote returns invalid credentials redirect."""
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "wrong_password")

    session.request.side_effect = [
        make_mock_response(200, "0"),                                      # POST security-check
        make_mock_response(302, "", {"Location": "/manager/login?error=1"}), # POST login fail redirect
    ]

    with pytest.raises(ClimoteAuthError):
        await api.login()

@pytest.mark.asyncio
async def test_get_status_cached_double_json():
    """Test retrieving status without GSM forcing, resolving escaped double-JSON."""
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"

    # Escaped JSON inside a string, as observed in live captures
    double_json_str = '"{\\"unit_time\\":\\"12:00\\",\\"zone1\\":{\\"burner\\":1,\\"temperature\\":\\"21\\",\\"thermostat\\":20}}"'

    session.request.return_value = make_mock_response(200, double_json_str)

    status = await api.get_status(force_gsm=False)

    assert status["unit_time"] == "12:00"
    assert status["zone1"]["burner"] == 1
    assert status["zone1"]["temperature"] == "21"
    assert status["zone1"]["thermostat"] == 20

@pytest.mark.asyncio
async def test_get_status_gsm_wait_loop():
    """Test retrieving status with GSM force, verifying async wait polling."""
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"

    success_data = {
        "unit_time": "12:05",
        "zone1": {"burner": 1, "timeRemaining": 30, "status": "5", "temperature": "22", "thermostat": 21}
    }

    session.request.side_effect = [
        make_mock_response(200, '"awaiting response"'),       # GET get-status?force=1
        make_mock_response(200, "0"),                          # GET waiting-get-status-response (attempt 1)
        make_mock_response(200, json.dumps(success_data)),     # GET waiting-get-status-response (attempt 2)
    ]

    with patch("asyncio.sleep", AsyncMock()):
        status = await api.get_status(force_gsm=True)

    assert status["unit_time"] == "12:05"
    assert status["zone1"]["status"] == "5"
    assert status["zone1"]["timeRemaining"] == 30

@pytest.mark.asyncio
async def test_set_boost_success():
    """Test triggering a boost and verifying the wait-for-delivery-report OK state."""
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"

    session.request.side_effect = [
        make_mock_response(200, ""),          # POST boost
        make_mock_response(200, "PENDING"),   # GET delivery report (attempt 1)
        make_mock_response(200, "OK"),        # GET delivery report (attempt 2)
    ]

    with patch("asyncio.sleep", AsyncMock()):
        result = await api.set_boost(zone_id=1, duration_hours=1.0)

    assert result is True


FORM_1_3 = '<input name="temp-set-input[1]"><input name="temp-set-input[3]">'
FORM_1_2_3 = FORM_1_3.replace('<input name="temp-set-input[3]">', '<input name="temp-set-input[2]"><input name="temp-set-input[3]">')

@pytest.mark.asyncio
async def test_set_temperature_payload():
    """The temperature POST matches the portal's form and never leaks the zone-2 field."""
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"

    session.request.side_effect = [
        make_mock_response(200, FORM_1_3),     # GET temperature page (which zones exist)
        make_mock_response(200, ""),           # POST temperature (no delivery poll follows)
    ]

    with patch("asyncio.sleep", AsyncMock()):
        result = await api.set_temperature(zone_id=1, temperature=19.3)

    assert result is True
    assert session.request.call_count == 2
    post = session.request.call_args_list[1]
    assert post.args[:2] == ("POST", f"{BASE_URL}/manager/temperature")
    assert post.kwargs["data"] == {
        "temp-set-input[1]": 19.5,
        "temp-set-input[3]": "nochange",
        "do": "Set",
        "cs_token_rf": "csrf123",
    }


@pytest.mark.asyncio
async def test_set_temperature_rejects_bad_input():
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"

    with pytest.raises(ValueError):
        await api.set_temperature(zone_id=1, temperature=40)  # too hot for a room
    with pytest.raises(ValueError):
        await api.set_temperature(zone_id=3, temperature=71)  # too hot even for water
    session.request.assert_not_called()


@pytest.mark.asyncio
async def test_set_temperature_zone_not_on_form():
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"
    session.request.side_effect = [make_mock_response(200, FORM_1_3)]

    with pytest.raises(ValueError):
        await api.set_temperature(zone_id=2, temperature=19)


@pytest.mark.asyncio
async def test_set_temperature_zone_2_when_form_has_it():
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"
    session.request.side_effect = [make_mock_response(200, FORM_1_2_3), make_mock_response(200, "")]

    assert await api.set_temperature(zone_id=2, temperature=19) is True
    data = session.request.call_args_list[1].kwargs["data"]
    assert data["temp-set-input[2]"] == 19
    assert data["temp-set-input[1]"] == "nochange"
    assert data["temp-set-input[3]"] == "nochange"


@pytest.mark.asyncio
async def test_set_temperature_water_zone_allows_65():
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"
    session.request.side_effect = [make_mock_response(200, FORM_1_3), make_mock_response(200, "")]

    assert await api.set_temperature(zone_id=3, temperature=65) is True
    data = session.request.call_args_list[1].kwargs["data"]
    assert data["temp-set-input[3]"] == 65
    assert data["temp-set-input[1]"] == "nochange"
