import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    LOGGER,
    CONF_EMAIL,
    CONF_DEVICE_ID,
    CONF_PIN,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
)
from .api import ClimoteAPI
from .coordinator import ClimoteDataUpdateCoordinator

PLATFORMS = [Platform.CLIMATE, Platform.SWITCH, Platform.SENSOR, Platform.SELECT]

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
    )

    # Fetch initial state
    await coordinator.async_config_entry_first_refresh()

    # Store state
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    # Register platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
