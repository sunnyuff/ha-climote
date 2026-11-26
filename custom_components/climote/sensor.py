from typing import Any
from datetime import datetime, timezone

from homeassistant.components.sensor import RestoreSensor, SensorEntity, SensorStateClass, SensorDeviceClass
from homeassistant.const import UnitOfTemperature, UnitOfTime, EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER
from .coordinator import ClimoteDataUpdateCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climote sensors from a config entry."""
    coordinator: ClimoteDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities = []
    for zone_id in range(1, 4):
        entities.append(ClimoteBoostTimeRemainingSensor(coordinator, entry, zone_id))
        entities.append(ClimoteTemperatureSensor(coordinator, entry, zone_id))

    # Add a single diagnostic sensor for last successful poll
    entities.append(ClimoteLastUpdatedSensor(coordinator, entry))
    entities.append(ClimoteLastHubReplySensor(coordinator, entry))

    async_add_entities(entities)

class ClimoteSensorBase(CoordinatorEntity[ClimoteDataUpdateCoordinator], SensorEntity):
    """Base class for all Climote sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ClimoteDataUpdateCoordinator,
        entry: ConfigEntry,
        zone_id: int,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entry = entry
        self._zone_id = zone_id
        
        self.email = entry.title.lower()
        self.zone_name = coordinator.api.zone_labels.get(zone_id, f"Zone {zone_id}")

        self._attr_device_info = {
            "identifiers": {(DOMAIN, self.email)},
            "name": f"Climote Heating ({entry.title})",
            "manufacturer": "Climote",
            "model": "Climote GSM Hub",
        }

    @property
    def _zone_data(self) -> dict[str, Any]:
        """Helper to get current status data for this specific zone."""
        key = f"zone{self._zone_id}"
        return (self.coordinator.data or {}).get(key, {})

class ClimoteTemperatureSensor(ClimoteSensorBase):
    """Sensor reporting the current temperature inside a Climote Zone."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: ClimoteDataUpdateCoordinator,
        entry: ConfigEntry,
        zone_id: int,
    ) -> None:
        """Initialize the temperature sensor."""
        super().__init__(coordinator, entry, zone_id)
        self._attr_unique_id = f"{self.email}_zone_{zone_id}_temp"
        self._attr_name = f"{self.zone_name} Temperature"

    @property
    def native_value(self) -> float | None:
        """Return the current temperature sensor value."""
        try:
            temp = self._zone_data.get("temperature")
            # If the zone doesn't have a physical sensor connected, it reports "00" or 0.
            if temp is not None and float(temp) > 0:
                return float(temp)
        except (ValueError, TypeError):
            pass
        return None

class ClimoteBoostTimeRemainingSensor(ClimoteSensorBase):
    """Sensor reporting the remaining boost minutes for a Climote Zone."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-sand"

    def __init__(
        self,
        coordinator: ClimoteDataUpdateCoordinator,
        entry: ConfigEntry,
        zone_id: int,
    ) -> None:
        """Initialize the time remaining sensor."""
        super().__init__(coordinator, entry, zone_id)
        self._attr_unique_id = f"{self.email}_zone_{zone_id}_time_remaining"
        self._attr_name = f"{self.zone_name} Boost Time Remaining"

    @property
    def native_value(self) -> int | None:
        """Return the remaining boost minutes."""
        try:
            time_rem = self._zone_data.get("timeRemaining")
            if time_rem is not None:
                return int(time_rem)
        except (ValueError, TypeError):
            pass
        # Return 0 if not boosted
        return 0


class ClimoteLastUpdatedSensor(CoordinatorEntity[ClimoteDataUpdateCoordinator], SensorEntity):
    """Diagnostic sensor showing when the last successful Climote poll occurred."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-check-outline"

    def __init__(
        self,
        coordinator: ClimoteDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the last-updated sensor."""
        super().__init__(coordinator)
        self.entry = entry

        email = entry.title.lower()
        self._attr_unique_id = f"{email}_last_updated"
        self._attr_name = "Last Updated"

        self._attr_device_info = {
            "identifiers": {(DOMAIN, email)},
            "name": f"Climote Heating ({entry.title})",
            "manufacturer": "Climote",
            "model": "Climote GSM Hub",
        }

    @property
    def native_value(self) -> datetime | None:
        """Return the timestamp of the last successful coordinator update."""
        if self.coordinator.last_update_success:
            return self.coordinator.last_update_success_time
        return None


class ClimoteLastHubReplySensor(CoordinatorEntity[ClimoteDataUpdateCoordinator], RestoreSensor):
    """When the hub itself last answered a status read.

    Unlike "Last Updated", this does not move when the portal only returns its cached copy.
    """

    _attr_has_entity_name = True
    _attr_name = "Last Hub Reply"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:message-check-outline"

    def __init__(self, coordinator: ClimoteDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._restored = None
        email = entry.title.lower()
        self._attr_unique_id = f"{email}_last_hub_reply"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, email)},
            "name": f"Climote Heating ({entry.title})",
            "manufacturer": "Climote",
            "model": "Climote GSM Hub",
        }

    async def async_added_to_hass(self) -> None:
        """Keep the time from before a restart until the hub answers again."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None:
            self._restored = last.native_value

    @property
    def available(self) -> bool:
        """The time stays true when polls fail."""
        return True

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.api.health.last_reply or self._restored
