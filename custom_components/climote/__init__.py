import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change

from .const import (
    DOMAIN,
    LOGGER,
    CONF_EMAIL,
    CONF_DEVICE_ID,
    CONF_PIN,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    CONF_SLEEP_START,
    CONF_SLEEP_END,
    DEFAULT_SLEEP_TIME,
)
from .api import ClimoteAPI, ClimoteConnectionError
from .coordinator import ClimoteDataUpdateCoordinator

PLATFORMS = [Platform.CLIMATE, Platform.SWITCH, Platform.SENSOR, Platform.NUMBER, Platform.BINARY_SENSOR]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Climote Heating from a config entry."""
    LOGGER.debug("Setting up Climote entry: %s", entry.title)

    hass.data.setdefault(DOMAIN, {})

    # Create API Client
    session = async_get_clientsession(hass)
    api = ClimoteAPI(
        session,
        email=entry.data[CONF_EMAIL],
        device_id=entry.data[CONF_DEVICE_ID],
        pin=entry.data.get(CONF_PIN, ""),
    )

    # Login and auto-discover schedule details, labels, and CSRF token
    try:
        await api.login()
    except ClimoteConnectionError as err:
        # No network or portal down (say, just after a reboot): HA tries again later. A login sends
        # nothing to the hub, so retrying costs no SMS.
        raise ConfigEntryNotReady(f"Could not reach Climote: {err}") from err
    except Exception as err:
        LOGGER.error("Failed to authenticate with Climote servers during setup: %s", err)
        return False

    # Retrieve options or fallback to entry config / defaults
    poll_interval = entry.options.get(
        CONF_POLL_INTERVAL,
        entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    )

    # Set up the Data Coordinator
    coordinator = ClimoteDataUpdateCoordinator(
        hass,
        api,
        poll_interval_seconds=int(poll_interval),
        sleep_start=entry.options.get(CONF_SLEEP_START, DEFAULT_SLEEP_TIME),
        sleep_end=entry.options.get(CONF_SLEEP_END, DEFAULT_SLEEP_TIME),
    )

    # Fetch initial state. Not async_config_entry_first_refresh: if the hub is silent that retries the
    # whole setup over and over, and each try can cost an SMS. Instead the entities start unavailable
    # and the next scheduled poll tries again.
    await coordinator.async_refresh()

    # Store state
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    # Register platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Read the hub as soon as the sleep period ends, not up to a whole poll interval later.
    if coordinator.sleep_start != coordinator.sleep_end:

        async def _wake(_now) -> None:
            await coordinator.async_request_refresh()

        entry.async_on_unload(
            async_track_time_change(
                hass, _wake, hour=coordinator.sleep_end.hour, minute=coordinator.sleep_end.minute, second=0
            )
        )

    # Register listener for option updates
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Climote config entry."""
    LOGGER.debug("Unloading Climote entry: %s", entry.title)
    
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options listener."""
    LOGGER.debug("Climote entry options updated. Reloading entry.")
    await hass.config_entries.async_reload(entry.entry_id)
