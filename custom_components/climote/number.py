"""Number platform for Climote Heating — per-zone boost duration slider."""
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, LOGGER, DEFAULT_BOOST_DURATION
from .coordinator import ClimoteDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climote number entities from a config entry."""
    coordinator: ClimoteDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            ClimoteBoostDurationNumber(coordinator, entry, zone_id)
            for zone_id in range(1, 4)
        ]
    )


class ClimoteBoostDurationNumber(NumberEntity, RestoreEntity):
    """Number entity to control Boost Duration for a Climote Zone.

    Appears as a slider (0.5 – 8.0 hours in 0.25-hour steps) in the UI.
    Stored in hass.data so the boost switch and climate entity can read the
    selected duration for each zone independently.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0.25
    _attr_native_max_value = 8.0
    _attr_native_step = 0.25
    _attr_native_unit_of_measurement = "h"
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: ClimoteDataUpdateCoordinator,
        entry: ConfigEntry,
        zone_id: int,
    ) -> None:
        """Initialize the number entity."""
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

        # Default from global config entry options
        global_default = entry.options.get(
            "boost_duration", entry.data.get("boost_duration", DEFAULT_BOOST_DURATION)
        )
        self._attr_native_value = float(global_default)

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register in shared data store."""
        await super().async_added_to_hass()

        # Initialize durations dictionary if not present
        if "boost_durations" not in self.hass.data[DOMAIN][self.entry.entry_id]:
            self.hass.data[DOMAIN][self.entry.entry_id]["boost_durations"] = {}

        # Restore previous slider value
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                pass

        # Store initial value so switch/climate can read it
        self.hass.data[DOMAIN][self.entry.entry_id]["boost_durations"][self._zone_id] = (
            self._attr_native_value
        )

    async def async_set_native_value(self, value: float) -> None:
        """Update the boost duration value."""
        self._attr_native_value = value
        self.hass.data[DOMAIN][self.entry.entry_id]["boost_durations"][self._zone_id] = value
        self.async_write_ha_state()
