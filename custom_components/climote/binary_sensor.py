"""Binary sensor platform for Climote Heating: is the hub answering?"""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ClimoteDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Climote binary sensor from a config entry."""
    coordinator: ClimoteDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([ClimoteHubSilentSensor(coordinator, entry)])


class ClimoteHubSilentSensor(CoordinatorEntity[ClimoteDataUpdateCoordinator], BinarySensorEntity):
    """On while several status reads in a row have gone unanswered (possibly an SMS rate limit)."""

    _attr_has_entity_name = True
    _attr_name = "Hub Not Answering"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ClimoteDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        email = entry.title.lower()
        self._attr_unique_id = f"{email}_hub_not_answering"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, email)},
            "name": f"Climote Heating ({entry.title})",
            "manufacturer": "Climote",
            "model": "Climote GSM Hub",
        }

    @property
    def available(self) -> bool:
        """Stay available when polls fail: that is when this matters."""
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.api.health.silent
