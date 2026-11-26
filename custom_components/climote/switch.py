from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER, CONF_BOOST_DURATION, DEFAULT_BOOST_DURATION
from .coordinator import ClimoteDataUpdateCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climote switches from a config entry."""
    coordinator: ClimoteDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    async_add_entities(
        [
            ClimoteZoneBoostSwitch(coordinator, entry, zone_id)
            for zone_id in range(1, 4)
        ]
    )

class ClimoteZoneBoostSwitch(CoordinatorEntity[ClimoteDataUpdateCoordinator], SwitchEntity):
    """Switch entity to control Boost Mode for a Climote Zone."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:fire"

    def __init__(
        self,
        coordinator: ClimoteDataUpdateCoordinator,
        entry: ConfigEntry,
        zone_id: int,
    ) -> None:
        """Initialize the switch entity."""
        super().__init__(coordinator)
        self.entry = entry
        self._zone_id = zone_id
        
        email = entry.title.lower()
        self._attr_unique_id = f"{email}_zone_{zone_id}_boost"
        
        zone_name = coordinator.api.zone_labels.get(zone_id, f"Zone {zone_id}")
        self._attr_name = f"{zone_name} Boost"

        self._attr_device_info = {
            "identifiers": {(DOMAIN, email)},
            "name": f"Climote Heating ({entry.title})",
            "manufacturer": "Climote",
            "model": "Climote GSM Hub",
        }

    @property
    def _zone_data(self) -> dict[str, Any]:
        """Helper to get current status data for this specific zone."""
        key = f"zone{self._zone_id}"
        return (self.coordinator.data or {}).get(key, {})

    @property
    def is_on(self) -> bool:
        """Return True if boost is currently active."""
        # The state a command just asked for wins until the hub confirms it (see the coordinator).
        expected = self.coordinator.expected_boost(self._zone_id)
        if expected is not None:
            return expected
        # 5 means Boost is active (str() because the hub may send it as a number or a string)
        return str(self._zone_data.get("status")) == "5"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the boost switch."""
        # Read duration (hours) from the number entity state stored in hass.data
        durations = self.hass.data[DOMAIN][self.entry.entry_id].get("boost_durations", {})
        duration = durations.get(self._zone_id, DEFAULT_BOOST_DURATION)
        LOGGER.info("Turning ON Boost Switch for zone %d (duration %s hours)", self._zone_id, duration)

        success = await self.coordinator.api.set_boost(self._zone_id, float(duration))
        if success:
            # An immediate status read returns the state from before the hub got the command, so
            # show the boost now and let one read a minute later confirm it.
            self.coordinator.expect_boost(self._zone_id, True, float(duration))
            self.coordinator.schedule_confirmation()
        else:
            raise HomeAssistantError(f"Climote did not accept the boost command for {self._attr_name} (see the log)")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the boost switch."""
        LOGGER.info("Turning OFF Boost Switch for zone %d (cancelling boost)", self._zone_id)
        
        success = await self.coordinator.api.cancel_boost(self._zone_id)
        if success:
            self.coordinator.expect_boost(self._zone_id, False)
            self.coordinator.schedule_confirmation()
        else:
            raise HomeAssistantError(f"Climote did not accept the cancel command for {self._attr_name} (see the log)")
