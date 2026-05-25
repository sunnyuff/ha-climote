import asyncio
import json
import re
import aiohttp
import xmltodict

from .const import (
    BASE_URL,
    LOGGER,
    PATH_SECURITY_CHECK,
    PATH_LOGIN,
    PATH_INDEX,
    PATH_STATUS,
    PATH_WAIT_STATUS,
    PATH_BOOST,
    PATH_DELIVERY_REPORT,
    PATH_SCHEDULE,
)

class ClimoteConnectionError(Exception):
    """Exception raised when connection to Climote fails."""
    pass

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
            "X-Requested-With": "XMLHttpRequest" if method == "GET" and ("get-status" in path or "waiting" in path) else None
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

    async def _fetch_status(self, force_gsm: bool = False) -> dict:
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
                status_data = json.loads(clean_res)
        except ClimoteConnectionError:
            raise
        except Exception as err:
            LOGGER.error("Failed to parse status response: %s", err)
            raise ClimoteConnectionError("Status retrieval parse failure") from err

        # If we commanded a GSM force_gsm, the return value is `"awaiting response"`.
        # In this case we poll the async waiting endpoint.
        if force_gsm or status_data == "awaiting response":
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
                    return data
            except Exception as err:
                LOGGER.warning("Error during GSM poll attempt %d: %s", attempt, err)

        LOGGER.error("GSM polling timed out after %d attempts. Returning cached/stale data.", max_attempts)
        # Fallback: get cached status without forcing GSM
        return await self.get_status(force_gsm=False)

    async def _send_boost_payload(self, payload: dict) -> None:
        """Internal: POST a boost payload, retrying once after re-auth on failure."""
        try:
            await self._request(
                "POST",
                PATH_BOOST,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
        except ClimoteConnectionError:
            # Session may have expired — re-login and rebuild CSRF in payload
            LOGGER.info("Boost POST failed — re-authenticating and retrying…")
            self._csrf_token = None
            await self.login()
            payload["cs_token_rf"] = self._csrf_token
            await self._request(
                "POST",
                PATH_BOOST,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )

    async def set_boost(self, zone_id: int, duration_hours: float) -> bool:
        """Trigger boost for a specific zone and wait for delivery confirmation."""
        LOGGER.info("Boosting zone %d for %s hours", zone_id, duration_hours)

        if not self._csrf_token:
            await self.login()

        # Build payload dynamically: set duration for target zone, "nochange" for others
        payload = {
            f"zoneIds[{i}]": duration_hours if i == zone_id else "nochange"
            for i in range(1, 4)
        }
        payload["cs_token_rf"] = self._csrf_token

        try:
            await self._send_boost_payload(payload)
        except Exception as err:
            LOGGER.error("Failed to POST boost command: %s", err)
            return False

        # Wait for delivery report
        return await self._wait_for_delivery()

    async def cancel_boost(self, zone_id: int) -> bool:
        """Send stop boost command for a specific zone."""
        LOGGER.info("Cancelling boost for zone %d", zone_id)

        if not self._csrf_token:
            await self.login()

        # Build payload: "stop" for target zone, "nochange" for others
        payload = {
            f"zoneIds[{i}]": "stop" if i == zone_id else "nochange"
            for i in range(1, 4)
        }
        payload["cs_token_rf"] = self._csrf_token

        try:
            await self._send_boost_payload(payload)
        except Exception as err:
            LOGGER.error("Failed to POST stop boost command: %s", err)
            return False

        # Wait for delivery report
        return await self._wait_for_delivery()

    async def _wait_for_delivery(self) -> bool:
        """Poll wait-for-delivery-report until DELIVERED/OK is returned.
        
        Max wait: 30 attempts × 3s = 90 seconds. Accounts for weak GSM signal.
        """
        max_attempts = 30
        poll_delay = 3.0

        for attempt in range(1, max_attempts + 1):
            await asyncio.sleep(poll_delay)
            try:
                res = await self._request("GET", PATH_DELIVERY_REPORT)
                status = res.strip().upper()
                LOGGER.debug("Delivery status: %s (attempt %d/%d)", status, attempt, max_attempts)
                
                # Real API returns "DELIVERED" when command reaches the hub.
                # "OK" kept as a fallback in case the response ever changes.
                if status in ("OK", "DELIVERED"):
                    LOGGER.debug("Command delivered successfully to GSM hub (status=%s)", status)
                    return True
                elif status == "PENDING":
                    continue
                else:
                    LOGGER.warning("Unexpected delivery status: %s (attempt %d/%d)", status, attempt, max_attempts)
            except Exception as err:
                LOGGER.warning("Error waiting for delivery report on attempt %d: %s", attempt, err)

        LOGGER.error("Command delivery report timed out after %d attempts", max_attempts)
        return False
