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


class ConfigEntryNotReady(Exception):
    """Stand-in for HA's exception, which must be a real class to be raised."""


sys.modules['homeassistant.exceptions'].ConfigEntryNotReady = ConfigEntryNotReady
sys.modules['homeassistant.helpers'] = MagicMock()
sys.modules['homeassistant.util'] = MagicMock()
sys.modules['homeassistant.helpers.event'] = MagicMock()
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
    ClimoteHubTimeout,
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
    """A boost is one POST with no delivery poll (the portal's delivery report never completes)."""
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"

    session.request.side_effect = [
        make_mock_response(200, FORM_1_3),    # GET temperature page (which zones exist)
        make_mock_response(200, ""),          # POST boost (no delivery poll follows)
    ]

    with patch("asyncio.sleep", AsyncMock()):
        result = await api.set_boost(zone_id=3, duration_hours=1.0)

    assert result is True
    assert session.request.call_count == 2
    # Like the portal's own form: zones 1 and 3 only, no zoneIds[2].
    assert session.request.call_args_list[1].kwargs["data"] == {
        "zoneIds[1]": "nochange",
        "zoneIds[3]": 1.0,
        "cs_token_rf": "csrf123",
    }


@pytest.mark.asyncio
async def test_set_boost_rejects_a_zone_the_portal_does_not_have():
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"
    session.request.side_effect = [make_mock_response(200, FORM_1_3)]

    result = await api.set_boost(zone_id=2, duration_hours=1.0)

    assert result is False
    assert session.request.call_count == 1  # no POST


@pytest.mark.asyncio
async def test_cancel_boost_success():
    """Cancelling a boost is one POST with no delivery poll, so the caller can refresh the status."""
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"

    session.request.side_effect = [make_mock_response(200, FORM_1_3), make_mock_response(200, "")]

    with patch("asyncio.sleep", AsyncMock()):
        result = await api.cancel_boost(zone_id=3)

    assert result is True
    assert session.request.call_count == 2
    assert session.request.call_args_list[1].kwargs["data"] == {
        "zoneIds[1]": "nochange",
        "zoneIds[3]": "stop",
        "cs_token_rf": "csrf123",
    }


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


@pytest.mark.asyncio
async def test_status_not_json_names_the_body_and_retries_after_login():
    """A login page in place of the status is an expired session: re-login, retry, and say what came back."""
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "old"
    login_page = "<html>\n  <body>Please log in " + "x" * 500
    session.request.side_effect = [make_mock_response(200, login_page), make_mock_response(200, '{"zone1": {}}')]

    async def fake_login():
        api._csrf_token = "new"

    api.login = fake_login
    assert await api.get_status() == {"zone1": {}}

    session.request.side_effect = [make_mock_response(200, login_page)] * 2
    with pytest.raises(ClimoteConnectionError) as err:
        await api.get_status()
    assert "starts '<html> <body>Please log in" in str(err.value)
    assert len(str(err.value)) < 200  # a snippet, never the page


@pytest.mark.asyncio
async def test_temperature_form_zones_relogs_in_when_the_page_is_the_login_page():
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "old"
    session.request.side_effect = [make_mock_response(200, "<html>login</html>"), make_mock_response(200, FORM_1_3)]

    async def fake_login():
        api._csrf_token = "new"

    api.login = fake_login
    assert await api.temperature_form_zones() == (1, 3)
    assert api._csrf_token == "new"


@pytest.mark.asyncio
async def test_gsm_timeout_falls_back_to_cache_once_and_does_not_poll_again():
    """A silent hub gives up after one 90 s poll; it must not start another one."""
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"

    session.request.side_effect = (
        [make_mock_response(200, '"awaiting response"')]    # GET get-status?force=1
        + [make_mock_response(200, "0")] * 30               # 30 polls, hub never answers
        + [make_mock_response(200, '"awaiting response"')]  # the cached fallback: still waiting
    )

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(ClimoteHubTimeout):
            await api.get_status(force_gsm=True)

    assert session.request.call_count == 32  # 1 + 30 + 1, then it stops
    assert api.health.timeouts == 1  # one read, however many times the portal was asked


@pytest.mark.asyncio
async def test_gsm_timeout_returns_cached_status_when_the_portal_has_one():
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"
    cached = {"zone1": {"status": None}, "unit_time": "12:00"}

    session.request.side_effect = (
        [make_mock_response(200, '"awaiting response"')]
        + [make_mock_response(200, "0")] * 30
        + [make_mock_response(200, json.dumps(cached))]
    )

    with patch("asyncio.sleep", AsyncMock()):
        data = await api.get_status(force_gsm=True)

    assert data == cached
    assert api.last_read_stale
    assert api.health.timeouts == 1 and api.health.last_reply is None


# --- what a boost command asked for (boost_state.py) -------------------------------------------

from custom_components.climote.boost_state import BoostExpectations, is_boosting


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_is_boosting_accepts_a_number_or_a_string():
    assert is_boosting({"status": 5})
    assert is_boosting({"status": "5"})
    assert not is_boosting({"status": None})
    assert not is_boosting({})


def test_requested_boost_shows_on_until_the_hub_confirms():
    clock = FakeClock()
    b = BoostExpectations(clock)
    b.expect(3, True, 900)
    assert b.get(3) is True
    # A stale read straight after the command (status still None) must not flip it back.
    confirmed, contradicted = b.reconcile({"zone3": {"status": None, "timeRemaining": None}})
    assert confirmed == [] and contradicted == [(3, {"status": None, "timeRemaining": None})]
    assert b.get(3) is True
    # The hub catches up: expectation is dropped and the hub's own state takes over.
    confirmed, _ = b.reconcile({"zone3": {"status": 5}})
    assert confirmed == [3]
    assert b.get(3) is None


def test_requested_cancel_shows_off_until_the_hub_confirms():
    b = BoostExpectations(FakeClock())
    b.expect(3, False, 600)
    assert b.get(3) is False
    b.reconcile({"zone3": {"status": "5"}})  # stale read, still boosting
    assert b.get(3) is False
    confirmed, _ = b.reconcile({"zone3": {"status": None}})
    assert confirmed == [3] and b.get(3) is None


def test_unconfirmed_boost_lapses_when_the_boost_would_have_ended():
    clock = FakeClock()
    b = BoostExpectations(clock)
    b.expect(3, True, 900)  # the hub never reports it
    clock.now += 899
    assert b.get(3) is True
    clock.now += 2
    assert b.get(3) is None


def test_expectations_are_per_zone():
    b = BoostExpectations(FakeClock())
    b.expect(3, True, 900)
    assert b.get(1) is None
    b.reconcile({"zone1": {"status": None}, "zone3": {"status": None}})
    assert b.get(3) is True


# --- a requested temperature (setpoint_state.py) -----------------------------------------------

from datetime import time

from custom_components.climote.setpoint_state import SetpointExpectations, SETPOINT_SETTLE_SECONDS
from custom_components.climote.sleep_period import in_sleep_period, parse_time


def reading(value):
    return {"zone1": {"thermostat": value}}


def test_requested_temperature_survives_early_and_stale_reads():
    clock = FakeClock()
    s = SetpointExpectations(clock)
    s.expect(1, 18.0)
    assert s.reconcile(reading(20), fresh=True) == ([], [])  # too early to say
    clock.now += SETPOINT_SETTLE_SECONDS
    assert s.reconcile(reading(20), fresh=False) == ([], [])  # hub silent, cached copy
    assert s.get(1) == 18.0
    assert s.reconcile(reading("18"), fresh=True) == ([1], [])
    assert s.get(1) is None


def test_a_settled_fresh_read_overrules_the_requested_temperature():
    clock = FakeClock()
    s = SetpointExpectations(clock)
    s.expect(1, 19.0)
    clock.now += SETPOINT_SETTLE_SECONDS
    assert s.reconcile(reading(15), fresh=True) == ([], [(1, 19.0, 15)])
    assert s.get(1) is None


def test_a_read_without_the_zone_leaves_the_expectation():
    s = SetpointExpectations(FakeClock())
    s.expect(1, 19.0)
    assert s.reconcile({}, fresh=True) == ([], [])
    assert s.get(1) == 19.0


@pytest.mark.parametrize(
    "now, start, end, asleep",
    [
        ("22:59", "23:00", "08:00", False),
        ("23:00", "23:00", "08:00", True),
        ("03:00", "23:00", "08:00", True),
        ("08:00", "23:00", "08:00", False),
        ("13:00", "12:00", "14:00", True),
        ("15:00", "12:00", "14:00", False),
        ("00:00", "00:00", "00:00", False),
    ],
)
def test_sleep_period(now, start, end, asleep):
    assert in_sleep_period(parse_time(now), parse_time(start), parse_time(end)) is asleep


def test_parse_time_falls_back_to_midnight():
    assert parse_time(None) == parse_time("nonsense") == time(0, 0)


# --- setup (__init__.py) -----------------------------------------------------------------------

import custom_components.climote as integration


def setup_args(login_error):
    api = MagicMock()
    api.login = AsyncMock(side_effect=login_error)
    entry = MagicMock()
    entry.data = {"email": "test@email.com", "device_id": "1234567890"}
    return api, MagicMock(), entry


@pytest.mark.asyncio
async def test_setup_is_retried_when_the_portal_cannot_be_reached():
    api, hass, entry = setup_args(ClimoteConnectionError("no network"))
    with patch.object(integration, "ClimoteAPI", return_value=api):
        with pytest.raises(ConfigEntryNotReady):
            await integration.async_setup_entry(hass, entry)


@pytest.mark.asyncio
async def test_setup_gives_up_on_rejected_credentials():
    api, hass, entry = setup_args(ClimoteAuthError("bad pin"))
    with patch.object(integration, "ClimoteAPI", return_value=api):
        assert await integration.async_setup_entry(hass, entry) is False


# --- is the hub answering (hub_health.py) ------------------------------------------------------

from custom_components.climote.hub_health import HubHealth, PollAction, PROBE_SECONDS, SILENT_AFTER


def test_hub_counts_as_silent_only_after_several_unanswered_reads_in_a_row():
    h = HubHealth(FakeClock())
    for _ in range(SILENT_AFTER - 1):
        h.timeout()
    assert not h.silent and h.may_read()
    h.reply()  # one answer starts the count again
    h.timeout()
    assert not h.silent


def test_silent_hub_is_tested_every_few_hours_and_a_reply_restores_it():
    clock = FakeClock()
    changes = []
    h = HubHealth(clock)
    h.on_change = lambda: changes.append(h.silent)
    for _ in range(SILENT_AFTER):
        h.timeout()
    assert h.silent and not h.may_read()
    clock.now += PROBE_SECONDS - 3600
    assert not h.may_read()
    clock.now += 3599  # a poll timer a second early still counts
    assert h.may_read()
    h.timeout()  # the test read also goes unanswered: wait again
    assert not h.may_read()
    h.reply()
    assert not h.silent and h.may_read() and h.last_reply is not None
    assert changes[-2:] == [True, False]


@pytest.mark.asyncio
async def test_an_answered_gsm_read_records_a_hub_reply():
    session = MagicMock(spec=aiohttp.ClientSession)
    api = ClimoteAPI(session, "test@email.com", "password")
    api._csrf_token = "csrf123"
    session.request.side_effect = [
        # The portal double-encodes its status reply.
        make_mock_response(200, json.dumps(json.dumps("awaiting response"))),
        make_mock_response(200, json.dumps({"zone1": {}})),
    ]
    with patch("asyncio.sleep", AsyncMock()):
        await api.get_status()
    assert api.health.last_reply is not None and api.health.timeouts == 0


@pytest.mark.parametrize("has_data, asleep, action", [
    (True, False, PollAction.READ),
    (False, False, PollAction.READ),
    (True, True, PollAction.SKIP),
    (False, True, PollAction.READ),  # HA starting at night
])
def test_poll_action_while_the_hub_answers(has_data, asleep, action):
    assert HubHealth(FakeClock()).poll_action(has_data, asleep) is action


@pytest.mark.parametrize("has_data", [True, False])
def test_silent_hub_is_left_alone_then_tested_with_a_forced_read(has_data):
    clock = FakeClock()
    h = HubHealth(clock)
    for _ in range(SILENT_AFTER):
        h.timeout()
    assert h.poll_action(has_data, asleep=False) is PollAction.SKIP
    assert h.poll_action(has_data, asleep=True) is PollAction.SKIP
    clock.now += PROBE_SECONDS
    assert h.poll_action(has_data, asleep=False) is PollAction.TEST
    assert h.poll_action(has_data, asleep=True) is (PollAction.SKIP if has_data else PollAction.TEST)
