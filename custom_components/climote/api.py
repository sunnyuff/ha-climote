import asyncio
import json
import re
import aiohttp
import xmltodict

from .hub_health import HubHealth
from .const import (
    BASE_URL,
    LOGGER,
    PATH_SECURITY_CHECK,
    PATH_LOGIN,
    PATH_INDEX,
    PATH_STATUS,
    PATH_WAIT_STATUS,
    PATH_BOOST,
    PATH_TEMPERATURE,
    PATH_SCHEDULE,
)

# Allowed setpoints per zone: room thermostats, and zone 3 (hot water) which runs far hotter.
ROOM_TEMP_LIMITS = (5.0, 32.0)
WATER_TEMP_LIMITS = (5.0, 70.0)
WATER_ZONE = 3


def temp_limits(zone_id: int) -> tuple[float, float]:
    """Return (min, max) setpoint for a zone."""
    return WATER_TEMP_LIMITS if zone_id == WATER_ZONE else ROOM_TEMP_LIMITS

class ClimoteConnectionError(Exception):
    """Exception raised when connection to Climote fails."""
    pass

class ClimoteHubTimeout(Exception):
    """The hub did not answer a status request in time.

    Deliberately not a ClimoteConnectionError: get_status retries those after a fresh login, which
    would start another SMS and another 90 s poll.
    """


class ClimoteAuthError(Exception):
    """Exception raised when authentication fails."""
    pass

class ClimoteAPI:
    """Async Client for Climote Cloud web portal."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        device_id: str,
        pin: str = "",
    ):
        """Initialize the Climote API client."""
        self._session = session
        self._email = email.strip()
        self._device_id = device_id.strip()  # Climote Device No. (10-digit hub ID)
        self._pin = pin.strip()
        self._csrf_token = None
        self._schedule_id = None
        self.gsm_reads = 0  # hub round trips since HA started, for the log line in _fetch_status
        self.last_read_stale = False  # True when the hub was silent and the portal's cache was returned
        self.health = HubHealth()  # is the hub answering reads?
        self.zone_labels = {1: "Zone 1", 2: "Zone 2", 3: "Zone 3"}

    @property
    def schedule_id(self) -> str:
        """Return the discovered heating schedule ID."""
        return self._schedule_id

    async def _request(
        self,
        method: str,
        path: str,
        data: dict = None,
        params: dict = None,
        headers: dict = None,
        allow_redirects: bool = True,
    ) -> str:
        """Perform HTTP request with error handling and logging."""
        url = f"{BASE_URL}{path}"
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest" if method == "GET" and ("get-status" in path or "waiting" in path or "delivery-report" in path) else None
        }
        if headers:
            req_headers.update(headers)

        # Strip None headers
        req_headers = {k: v for k, v in req_headers.items() if v is not None}

        try:
            async with self._session.request(
                method,
                url,
                data=data,
                params=params,
                headers=req_headers,
                allow_redirects=allow_redirects,
                timeout=20,
            ) as response:
                text = await response.text()
                
                # Check for standard HTTP errors
                if response.status not in (200, 302):
                    LOGGER.error(
                        "Climote HTTP request failed: %s %s -> Status: %d, Response: %s",
                        method, path, response.status, text[:200]
                    )
                    raise ClimoteConnectionError(f"HTTP error status: {response.status}")
                
                return text
        except asyncio.TimeoutError as err:
            LOGGER.error("Climote HTTP timeout calling %s %s", method, path)
            raise ClimoteConnectionError("Request timeout calling Climote cloud") from err
        except aiohttp.ClientError as err:
            LOGGER.error("Climote HTTP client error calling %s %s: %s", method, path, err)
            raise ClimoteConnectionError(f"Network error: {err}") from err

    async def login(self) -> bool:
        """Perform security pre-check, authenticates, and extracts CSRF + Schedule IDs."""
        LOGGER.debug("Starting Climote login process for email: %s", self._email)

        # 1. Pre-login PIN security check
        try:
            sec_check = await self._request(
                "POST",
                PATH_SECURITY_CHECK,
                data={"email": self._email}
            )
            # Response is '1' if PIN is required, '0' if not
            pin_required = sec_check.strip() == "1"
            LOGGER.debug("Climote security check returned pin_required=%s", pin_required)
            if pin_required and not self._pin:
                LOGGER.warning("Climote reports PIN is required but none was provided.")
        except Exception as err:
            LOGGER.error("Climote pre-login check failed: %s", err)
            raise ClimoteConnectionError("Pre-login security check failed") from err

        # 2. Form POST Login
        # Note: the HTML form field names are counter-intuitive (discovered via live capture):
        #   'password' field  ← receives the user's Email Address
        #   'username' field  ← receives the Climote Device No. (e.g. 1234567890)
        login_payload = {
            "password": self._email,      # HTML field named 'password' holds the email
            "username": self._device_id,  # HTML field named 'username' holds the Device No.
            "passcode": self._pin,        # Optional security PIN
            "loginBtn": "Log in"
        }

        try:
            # We don't follow redirect automatically to handle 302 easily
            url = f"{BASE_URL}{PATH_LOGIN}"
            async with self._session.request(
                "POST",
                url,
                data=login_payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"{BASE_URL}/manager/login"
                },
                allow_redirects=False,
                timeout=15
            ) as response:
                # Check for successful login redirect
                if response.status == 302:
                    location = response.headers.get("Location", "")
                    if "login" in location or "error" in location:
                        LOGGER.error("Climote login rejected. Check credentials.")
                        raise ClimoteAuthError("Invalid email, password, or security PIN")
                    LOGGER.debug("Climote login successfully redirected to %s", location)
                elif response.status == 200:
                    # Some login forms render error page on 200
                    text = await response.text()
                    if "invalid" in text.lower() or "error" in text.lower() or "login" in response.url.path:
                        LOGGER.error("Climote login form returned success status but login failed")
                        raise ClimoteAuthError("Invalid email, password, or security PIN")
                else:
                    raise ClimoteConnectionError(f"Unexpected login response status: {response.status}")
        except ClimoteAuthError:
            raise
        except Exception as err:
            LOGGER.error("Climote login request failed: %s", err)
            raise ClimoteConnectionError("Authentication connection failure") from err

        # 3. Load index page to extract tokens and schedules
        try:
            index_html = await self._request("GET", PATH_INDEX)
            
            # Extract CSRF token (cs_token_rf)
            csrf_match = re.search(r'name="cs_token_rf"\s+value="([^"]+)"', index_html)
            if csrf_match:
                self._csrf_token = csrf_match.group(1)
                LOGGER.debug("Discovered Climote CSRF Token: %s", self._csrf_token)
            else:
                LOGGER.error("Failed to find Climote CSRF token in index page HTML")
                raise ClimoteAuthError("CSRF extraction failed")

            # Extract heatingScheduleId
            schedule_match = re.search(r'heatingScheduleId=(\d+)', index_html)
            if schedule_match:
                self._schedule_id = schedule_match.group(1)
                LOGGER.debug("Discovered Climote Schedule ID: %s", self._schedule_id)
            else:
                LOGGER.warning("Could not auto-discover Climote schedule ID in index HTML")

        except Exception as err:
            LOGGER.error("Failed to load Climote dashboard after login: %s", err)
            raise ClimoteConnectionError("Dashboard initialization failed") from err

        # 4. Fetch dynamic zone labels if schedule ID is available
        if self._schedule_id:
            try:
                await self.fetch_zone_labels()
            except Exception as err:
                LOGGER.warning("Could not fetch custom zone labels, using defaults: %s", err)

        return True

    async def fetch_zone_labels(self) -> dict[int, str]:
        """Fetch XML schedule and parse zone labels (e.g. 1 -> Living)."""
        if not self._schedule_id:
            return self.zone_labels

        LOGGER.debug("Fetching dynamic zone labels for schedule ID: %s", self._schedule_id)
        try:
            xml_data = await self._request(
                "GET",
                PATH_SCHEDULE,
                params={"heatingScheduleId": self._schedule_id, "startday": "sunday"}
            )
            
            parsed = xmltodict.parse(xml_data)
            zone_info = parsed.get("data", {}).get("zoneInfo", {}).get("zone")
            
            if zone_info:
                # If only one zone is returned, xmltodict returns a dict instead of a list
                if not isinstance(zone_info, list):
                    zone_info = [zone_info]
                
                new_labels = {}
                for zone in zone_info:
                    z_id = int(zone.get("@id"))
                    label = zone.get("label", f"Zone {z_id}")
                    new_labels[z_id] = label
                
                self.zone_labels.update(new_labels)
                LOGGER.debug("Discovered zone labels: %s", self.zone_labels)
        except Exception as err:
            LOGGER.warning("Failed to parse zone labels from XML schedule: %s", err)

        return self.zone_labels

    async def get_status(self, force_gsm: bool = False) -> dict:
        """Fetch zone status with automatic session recovery.

        If the first attempt fails (e.g. expired session returns an empty body),
        the session is invalidated, a fresh login is performed, and the request
        is retried once.  This prevents the integration from becoming permanently
        stuck after a session timeout.
        """
        LOGGER.debug("Fetching Climote status (force_gsm=%s)", force_gsm)
        self.last_read_stale = False

        # Ensure we are logged in by checking CSRF token
        if not self._csrf_token:
            LOGGER.debug("Not logged in. Performing login prior to get_status.")
            await self.login()

        try:
            return await self._fetch_status(force_gsm)
        except ClimoteConnectionError:
            # Session likely expired — force re-login and retry once
            LOGGER.info(
                "Climote status fetch failed — session may have expired. "
                "Re-authenticating and retrying…"
            )
            self._csrf_token = None
            await self.login()
            return await self._fetch_status(force_gsm)

    async def _fetch_status(self, force_gsm: bool = False, poll_if_awaiting: bool = True) -> dict:
        """Internal: fetch zone status (single attempt, no retry)."""
        force_val = "1" if force_gsm else "0"
        try:
            status_res = await self._request(
                "GET",
                PATH_STATUS,
                params={"force": force_val}
            )

            # get-status returns an escaped JSON string inside a string. Parse twice.
            clean_res = status_res.strip()

            # Empty body is a strong signal of session expiry
            if not clean_res:
                raise ClimoteConnectionError(
                    "Empty response from Climote API — session likely expired"
                )

            if clean_res.startswith('"') and clean_res.endswith('"'):
                try:
                    parsed_str = json.loads(clean_res)
                    # If this yields a valid JSON structure, parse it
                    if isinstance(parsed_str, str):
                        status_data = json.loads(parsed_str)
                    else:
                        status_data = parsed_str
                except Exception:
                    status_data = clean_res
            else:
                try:
                    status_data = json.loads(clean_res)
                except ValueError as err:
                    # Usually the login page, served in place of the status once the session has
                    # expired. get_status re-authenticates and retries; the snippet (first 80 characters,
                    # never the whole page) shows what the portal sent if the retry fails too.
                    snippet = " ".join(clean_res.split())[:80]
                    raise ClimoteConnectionError(
                        f"Status reply is not JSON ({len(clean_res)} chars, starts {snippet!r})"
                    ) from err
        except ClimoteConnectionError:
            raise
        except Exception as err:
            LOGGER.error("Failed to parse status response: %s", err)
            raise ClimoteConnectionError("Status retrieval parse failure") from err

        # If we commanded a GSM force_gsm, the return value is `"awaiting response"`.
        # In this case we poll the async waiting endpoint.
        if force_gsm or (poll_if_awaiting and status_data == "awaiting response"):
            # Greppable, one line per hub SMS round trip: count these to see what the allowance is spent on.
            self.gsm_reads += 1
            LOGGER.info(
                "Climote GSM read #%d since start (reason: %s)",
                self.gsm_reads,
                "forced by caller" if force_gsm else "portal answered 'awaiting response' to a plain poll",
            )
            LOGGER.debug("Status requires GSM fetch. Polling wait endpoint...")
            return await self._poll_gsm_status()

        return status_data

    async def _poll_gsm_status(self) -> dict:
        """Poll the waiting-get-status-response endpoint until GSM answers.
        
        Max wait: 30 attempts × 3s = 90 seconds. Accounts for weak GSM signal areas.
        """
        max_attempts = 30
        poll_delay = 3.0

        for attempt in range(1, max_attempts + 1):
            await asyncio.sleep(poll_delay)
            LOGGER.debug("GSM Poll Attempt %d/%d", attempt, max_attempts)
            try:
                poll_res = await self._request("GET", PATH_WAIT_STATUS)
                
                # Check for empty or busy response (live log showed '0' when waiting)
                clean_res = poll_res.strip()
                if clean_res == "0":
                    continue
                
                data = json.loads(clean_res)
                if isinstance(data, dict) and "zone1" in data:
                    LOGGER.debug("GSM update completed successfully on attempt %d", attempt)
                    self.health.reply()
                    return data
            except Exception as err:
                LOGGER.warning("Error during GSM poll attempt %d: %s", attempt, err)

        LOGGER.error("GSM polling timed out after %d attempts. Falling back to the cached status.", max_attempts)
        self.health.timeout()
        # Fallback: one cached read that never starts another poll. It used to call get_status, and
        # when the portal answered "awaiting response" again that began another 90 s poll, and so on
        # for as long as the hub stayed silent.
        data = await self._fetch_status(force_gsm=False, poll_if_awaiting=False)
        if not isinstance(data, dict) or "zone1" not in data:
            raise ClimoteHubTimeout(
                f"Hub did not answer within {int(max_attempts * poll_delay)} s and the portal has no cached status"
            )
        self.last_read_stale = True
        return data

    async def _send_form(self, path: str, payload: dict) -> None:
        """Internal: POST a form payload, retrying once after re-auth on failure."""
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            await self._request("POST", path, data=payload, headers=headers)
        except ClimoteConnectionError:
            # Session may have expired: re-login and rebuild the CSRF token in the payload
            LOGGER.info("POST to %s failed, re-authenticating and retrying", path)
            self._csrf_token = None
            await self.login()
            payload["cs_token_rf"] = self._csrf_token
            await self._request("POST", path, data=payload, headers=headers)

    async def _send_boost_payload(self, payload: dict) -> None:
        """Internal: POST a boost payload, retrying once after re-auth on failure."""
        await self._send_form(PATH_BOOST, payload)

    async def temperature_form_zones(self) -> tuple[int, ...]:
        """Return the zones the portal's temperature form has a field for.

        Read from the page itself (inputs named temp-set-input[N]) because it depends on the
        installation: one account's form lists zones 1 and 3 only. Falls back to all three
        zones if the page cannot be read or lists none.
        """
        try:
            for attempt in (1, 2):
                html = await self._request("GET", PATH_TEMPERATURE)
                zones = sorted({int(n) for n in re.findall(r"temp-set-input\[(\d+)\]", html)})
                if zones:
                    return tuple(zones)
                if attempt == 1:
                    # An expired session gets the login page (HTTP 200) instead of the form.
                    LOGGER.info("No temp-set-input fields on the temperature page, re-authenticating")
                    self._csrf_token = None
                    await self.login()
            LOGGER.warning("No temp-set-input fields found on the temperature page")
        except ClimoteConnectionError as err:
            LOGGER.warning("Could not read the temperature page: %s", err)
        return (1, 2, 3)

    async def set_temperature(self, zone_id: int, temperature: float) -> bool:
        """Set a zone's target temperature (POST accepted; delivery is not awaited).

        Sends the portal's own form (POST /manager/temperature): the target zone gets the value,
        every other zone the form lists gets "nochange". The temperature is validated against the
        zone's limits and rounded to 0.5 C.
        """
        low, high = temp_limits(zone_id)
        if not low <= float(temperature) <= high:
            raise ValueError(f"Temperature {temperature} outside {low}-{high} for zone {zone_id}")

        if not self._csrf_token:
            await self.login()

        form_zones = await self.temperature_form_zones()
        if zone_id not in form_zones:
            raise ValueError(f"Zone {zone_id} is not on the portal's temperature form {form_zones}")

        value = round(float(temperature) * 2) / 2
        LOGGER.info("Climote GSM command: set zone %d to %s (costs one SMS)", zone_id, value)

        payload = {
            f"temp-set-input[{i}]": value if i == zone_id else "nochange"
            for i in form_zones
        }
        payload["do"] = "Set"
        payload["cs_token_rf"] = self._csrf_token

        try:
            await self._send_form(PATH_TEMPERATURE, payload)
        except Exception as err:
            LOGGER.error("Failed to POST temperature command: %s", err)
            return False

        # No delivery wait: on this portal the delivery-report poll returns an empty body and the
        # "Complete" step never turns green, even though the setpoint changes. The caller confirms
        # by reading the setpoint back from the hub instead.
        return True

    async def set_boost(self, zone_id: int, duration_hours: float) -> bool:
        """Trigger boost for a specific zone (one POST; the caller reads the status back)."""
        LOGGER.info("Climote GSM command: boost zone %d for %s hours (costs one SMS)", zone_id, duration_hours)

        if not self._csrf_token:
            await self.login()

        # Only the zones the portal's own form has (zones 1 and 3 here; the browser sends no
        # zoneIds[2]), "nochange" for every zone but the one being boosted.
        form_zones = await self.temperature_form_zones()
        if zone_id not in form_zones:
            LOGGER.error("Zone %d is not on the portal's form %s, so it cannot be boosted", zone_id, form_zones)
            return False
        payload = {
            f"zoneIds[{i}]": duration_hours if i == zone_id else "nochange"
            for i in form_zones
        }
        payload["cs_token_rf"] = self._csrf_token

        try:
            await self._send_boost_payload(payload)
        except Exception as err:
            LOGGER.error("Failed to POST boost command: %s", err)
            return False

        # No delivery wait, same as set_temperature: on this portal the delivery report never says
        # DELIVERED, so waiting 90 s always ended in False and the caller skipped its status refresh,
        # leaving the boost switch showing Off. The caller confirms by reading the hub's status.
        return True

    async def cancel_boost(self, zone_id: int) -> bool:
        """Send stop boost command for a specific zone."""
        LOGGER.info("Climote GSM command: cancel boost on zone %d (costs one SMS)", zone_id)

        if not self._csrf_token:
            await self.login()

        # Same zones as the portal's form (see set_boost): "stop" for the target, "nochange" for the rest.
        form_zones = await self.temperature_form_zones()
        if zone_id not in form_zones:
            LOGGER.error("Zone %d is not on the portal's form %s, so its boost cannot be cancelled", zone_id, form_zones)
            return False
        payload = {
            f"zoneIds[{i}]": "stop" if i == zone_id else "nochange"
            for i in form_zones
        }
        payload["cs_token_rf"] = self._csrf_token

        try:
            await self._send_boost_payload(payload)
        except Exception as err:
            LOGGER.error("Failed to POST stop boost command: %s", err)
            return False

        # No delivery wait (see set_boost).
        return True
