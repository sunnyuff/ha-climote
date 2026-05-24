"""Select platform for Climote Heating."""
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, LOGGER
from .coordinator import ClimoteDataUpdateCoordinator

BOOST_OPTIONS_MAP = {
    "30 minutes": 0.5,
    "45 minutes": 0.75,
    "1 hour": 1.0,
    "1 hour 15 minutes": 1.25,
    "1 hour 30 minutes": 1.5,
    "1 hour 45 minutes": 1.75,
    "2 hours": 2.0,
    "3 hours": 3.0,
    "4 hours": 4.0,
    "6 hours": 6.0,
    "8 hours": 8.0,
}
REVERSE_BOOST_OPTIONS = {v: k for k, v in BOOST_OPTIONS_MAP.items()}

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climote select from a config entry."""
    coordinator: ClimoteDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            ClimoteBoostDurationSelect(coordinator, entry, zone_id)
            for zone_id in range(1, 4)
        ]
    )

class ClimoteBoostDurationSelect(SelectEntity, RestoreEntity):
    """Select entity to control Boost Duration for a Climote Zone."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-outline"
    _attr_options = list(BOOST_OPTIONS_MAP.keys())

    def __init__(
        self,
        coordinator: ClimoteDataUpdateCoordinator,
        entry: ConfigEntry,
        zone_id: int,
    ) -> None:
        """Initialize the select entity."""
        self.coordinator = coordinator
        self.entry = entry
        self._zone_id = zone_id
        
        email = entry.title.lower()
        self._attr_unique_id = f"{email}_zone_{zone_id}_boost_duration"
        
        zone_name = coordinator.api.zone_labels.get(zone_id, f"Zone {zone_id}")
        self._attr_name = f"{zone_name} Boost Duration"

        self._attr_device_info = {
            "identifiers": {(DOMAIN, email)},
            "name": f"Climote Heating ({entry.title})",
            "manufacturer": "Climote",
            "model": "Climote GSM Hub",
        }
        
        # Fallback default from global config entry options
        global_default = entry.options.get("boost_duration", entry.data.get("boost_duration", 1.0))
        self._attr_current_option = REVERSE_BOOST_OPTIONS.get(float(global_default), "1 hour")

    async def async_added_to_hass(self) -> None:
        """Restore previous state."""
        await super().async_added_to_hass()
        
        # Initialize durations dictionary if not present
        if "boost_durations" not in self.hass.data[DOMAIN][self.entry.entry_id]:
            self.hass.data[DOMAIN][self.entry.entry_id]["boost_durations"] = {}
            
        last_state = await self.async_get_last_state()
        if last_state and last_state.state in self._attr_options:
            self._attr_current_option = last_state.state
            
        # Store initial state so switch can read it
        self.hass.data[DOMAIN][self.entry.entry_id]["boost_durations"][self._zone_id] = self._attr_current_option

    async def async_select_option(self, option: str) -> None:
        """Update the current selected option."""
        self._attr_current_option = option
        self.hass.data[DOMAIN][self.entry.entry_id]["boost_durations"][self._zone_id] = option
        self.async_write_ha_state()
