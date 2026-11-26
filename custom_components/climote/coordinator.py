from collections.abc import Callable
import time
from datetime import timedelta
import async_timeout

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER, DEFAULT_POLL_INTERVAL, DEFAULT_SLEEP_TIME
from .api import ClimoteAPI, ClimoteConnectionError, ClimoteAuthError
from .boost_state import BoostExpectations
from .hub_health import PROBE_SECONDS, PollAction
from .setpoint_state import SetpointExpectations, SETPOINT_SETTLE_SECONDS
from .sleep_period import in_sleep_period, parse_time

# A boost or cancel is an SMS to the hub, and the hub needs a while to act on it: a status read a
# second later still returns the old state. So after a command the entities show the state that was
# asked for, and one read confirms it this long afterwards.
BOOST_CONFIRM_DELAY = 60
# How long a requested "boost off" is believed without the hub agreeing. A requested "boost on" is
# believed for the boost's own duration.
BOOST_OFF_GRACE = 600
# A poll can log in again and then wait 90 s for the hub.
POLL_TIMEOUT = 150


class ClimoteDataUpdateCoordinator(DataUpdateCoordinator[dict]):
    """Class to manage fetching Climote heating data from cloud."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: ClimoteAPI,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL,
        sleep_start: str = DEFAULT_SLEEP_TIME,
        sleep_end: str = DEFAULT_SLEEP_TIME,
    ) -> None:
        """Initialize the coordinator."""
        self.api = api
        
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval_seconds),
        )
        # HA's coordinator only has a success flag, no timestamp; the Last Updated sensor reads this.
        self.last_update_success_time = None
        self._boosts = BoostExpectations()
        self._setpoints = SetpointExpectations()
        self.sleep_start = parse_time(sleep_start)
        self.sleep_end = parse_time(sleep_end)
        self._cancel_confirm = None
        self._confirm_due = 0.0
        self._cancel_expiry: dict[int, Callable[[], None]] = {}
        self._hub_was_silent = False
        api.health.on_change = self._hub_health_changed

    @callback
    def _hub_health_changed(self) -> None:
        health = self.api.health
        if health.silent != self._hub_was_silent:
            self._hub_was_silent = health.silent
            if health.silent:
                LOGGER.warning(
                    "The hub has not answered %d reads in a row (rate limited, off or no signal?); "
                    "test reads are now at least %d hours apart",
                    health.timeouts, PROBE_SECONDS // 3600,
                )
            else:
                LOGGER.info("The hub is answering again; normal polling resumes")
        # A failed read does not make HA refresh the entities, so do it here.
        self.async_update_listeners()

    def expect_boost(self, zone_id: int, on: bool, duration_hours: float = 0.0) -> None:
        """Remember that a boost on/off command was just sent, and show that state meanwhile."""
        ttl = duration_hours * 3600 if on else BOOST_OFF_GRACE
        self._boosts.expect(zone_id, on, ttl)
        self.async_update_listeners()

        # Entities only re-read their state when told to. Without this, an expectation that lapses
        # (a boost the hub never reports as status 5) would leave the switch showing On until the
        # next poll, which can be hours away.
        if zone_id in self._cancel_expiry:
            self._cancel_expiry.pop(zone_id)()

        @callback
        def _expired(_now) -> None:
            self._cancel_expiry.pop(zone_id, None)
            self.async_update_listeners()

        self._cancel_expiry[zone_id] = async_call_later(self.hass, ttl + 1, _expired)

    def expected_boost(self, zone_id: int) -> bool | None:
        """The boost state the last command asked for, or None once the hub confirmed it or it lapsed."""
        return self._boosts.get(zone_id)

    def expect_setpoint(self, zone_id: int, temperature: float, by_person: bool) -> None:
        """Show a just-sent temperature and read the hub once it has had time to apply it.

        In the sleep period only a change made by a person gets that read; others wait for the morning.
        """
        self._setpoints.expect(zone_id, temperature)
        self.async_update_listeners()
        self.schedule_confirmation(SETPOINT_SETTLE_SECONDS, override_sleep=by_person)

    def expected_setpoint(self, zone_id: int) -> float | None:
        """The temperature the last command asked for, until a read of the hub settles it."""
        return self._setpoints.get(zone_id)

    def _reconcile_expected(self, data: dict, forced: bool = False) -> None:
        reached, overruled = self._setpoints.reconcile(data, fresh=not self.api.last_read_stale)
        for zone_id in reached:
            LOGGER.info("Zone %d: hub confirms the requested temperature", zone_id)
        for zone_id, wanted, reported in overruled:
            LOGGER.warning("Zone %d: asked for %s C but the hub reports %s C", zone_id, wanted, reported)
        confirmed, contradicted = self._boosts.reconcile(data)
        for zone_id in confirmed:
            LOGGER.info("Zone %d: hub confirms the requested boost state", zone_id)
        for zone_id, zone in contradicted:
            # A regular poll soon after a command is expected to be stale; only the read made on
            # purpose a minute later says anything about whether the hub reports this boost at all.
            (LOGGER.warning if forced else LOGGER.debug)(
                "Zone %d: a boost command was sent but the hub reports status=%r timeRemaining=%r; "
                "keeping the requested state until the boost would have ended",
                zone_id, zone.get("status"), zone.get("timeRemaining"),
            )

    def schedule_confirmation(self, delay: float = BOOST_CONFIRM_DELAY, override_sleep: bool = False) -> None:
        """One forced hub read (one SMS) at least `delay` seconds from now.

        A read already waiting is folded into this one, at whichever of the two times is later.
        Skipped in the sleep period unless `override_sleep`; a read already waiting is left alone.
        """
        if not override_sleep and self._asleep():
            LOGGER.debug("Climote sleep period: no confirmation read")
            return
        now = time.monotonic()
        if self._cancel_confirm is not None:
            self._cancel_confirm()
            delay = max(delay, self._confirm_due - now)
        self._confirm_due = now + delay

        async def _confirm(_now) -> None:
            self._cancel_confirm = None
            if not self.api.health.may_read():
                LOGGER.debug("Hub not answering: no confirmation read")
                return
            try:
                await self.async_force_gsm_refresh()
            except UpdateFailed as err:
                LOGGER.warning("Confirmation read after a boost command failed: %s", err)

        self._cancel_confirm = async_call_later(self.hass, delay, _confirm)

    def _asleep(self) -> bool:
        return in_sleep_period(dt_util.now().time(), self.sleep_start, self.sleep_end)

    async def async_shutdown(self) -> None:
        if self._cancel_confirm is not None:
            self._cancel_confirm()
            self._cancel_confirm = None
        for cancel in self._cancel_expiry.values():
            cancel()
        self._cancel_expiry.clear()
        await super().async_shutdown()

    async def _async_update_data(self) -> dict:
        """Fetch data from Climote Cloud (regular cached polling)."""
        action = self.api.health.poll_action(has_data=bool(self.data), asleep=self._asleep())
        if action is PollAction.SKIP:
            LOGGER.debug("Climote: skipping this poll (sleep period, or hub not answering)")
            if not self.data or not self.last_update_success:
                raise UpdateFailed("Poll skipped, and there is no successful read to show")
            return self.data

        LOGGER.debug("Climote coordinator poll: %s", action.value)
        try:
            async with async_timeout.timeout(POLL_TIMEOUT):
                # A standard poll does not force a hub read; a test of a silent hub does.
                data = await self.api.get_status(force_gsm=action is PollAction.TEST)
                
                # Check if data is valid
                if not data or not isinstance(data, dict):
                    raise UpdateFailed("Received invalid response from Climote API")
                
                LOGGER.debug("Climote coordinator poll success: %s", data)
                self.last_update_success_time = dt_util.utcnow()
                self._reconcile_expected(data)
                return data
        except ClimoteAuthError as err:
            LOGGER.error("Climote authentication expired during polling: %s", err)
            raise UpdateFailed(f"Auth expired: {err}") from err
        except ClimoteConnectionError as err:
            LOGGER.warning("Climote connection error during polling: %s", err)
            raise UpdateFailed(f"Connection error: {err}") from err
        except Exception as err:
            LOGGER.error("Unexpected error in Climote coordinator polling: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def async_force_gsm_refresh(self) -> dict:
        """Trigger a real-time GSM refresh from the hub and update entities."""
        LOGGER.info("Triggering real-time GSM refresh on Climote hub...")
        try:
            # Command fresh SMS query (force_gsm=True) and wait for the hub
            data = await self.api.get_status(force_gsm=True)
            
            if data and isinstance(data, dict):
                # Update the coordinator's cached data and notify listeners immediately
                self.last_update_success_time = dt_util.utcnow()
                # Raw zone data at INFO, so what the hub says during a boost is in the normal log.
                LOGGER.info(
                    "Climote hub status after forced read: %s",
                    {k: v for k, v in data.items() if k.startswith("zone")},
                )
                self._reconcile_expected(data, forced=True)
                self.async_set_updated_data(data)
                return data
            else:
                raise UpdateFailed("Failed to fetch fresh GSM status from hub")
        except Exception as err:
            LOGGER.error("Failed to perform forced GSM update: %s", err)
            raise UpdateFailed(f"GSM refresh error: {err}") from err
