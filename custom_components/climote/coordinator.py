from datetime import timedelta
import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LOGGER, DEFAULT_POLL_INTERVAL
from .api import ClimoteAPI, ClimoteConnectionError, ClimoteAuthError

class ClimoteDataUpdateCoordinator(DataUpdateCoordinator[dict]):
    """Class to manage fetching Climote heating data from cloud."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: ClimoteAPI,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        """Initialize the coordinator."""
        self.api = api
        
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval_seconds),
        )

    async def _async_update_data(self) -> dict:
        """Fetch data from Climote Cloud (regular cached polling)."""
        LOGGER.debug("Climote coordinator polling cached state...")
        try:
            async with async_timeout.timeout(30):
                # Standard poll uses force_gsm=False to avoid expensive SMS
                data = await self.api.get_status(force_gsm=False)
                
                # Check if data is valid
                if not data or not isinstance(data, dict):
                    raise UpdateFailed("Received invalid response from Climote API")
                
                LOGGER.debug("Climote coordinator poll success: %s", data)
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
                self.async_set_updated_data(data)
                return data
            else:
                raise UpdateFailed("Failed to fetch fresh GSM status from hub")
        except Exception as err:
            LOGGER.error("Failed to perform forced GSM update: %s", err)
            raise UpdateFailed(f"GSM refresh error: {err}") from err
