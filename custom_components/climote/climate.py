from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER, DEFAULT_BOOST_DURATION
from .coordinator import ClimoteDataUpdateCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climote climate entities from a config entry."""
    coordinator: ClimoteDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    # We always have 3 zones on Climote
    async_add_entities(
        [
            ClimoteZoneClimate(coordinator, entry, zone_id)
            for zone_id in range(1, 4)
        ]
    )

class ClimoteZoneClimate(CoordinatorEntity[ClimoteDataUpdateCoordinator], ClimateEntity):
    """Representation of a Climote Heating Zone as a Climate Entity."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(
        self,
        coordinator: ClimoteDataUpdateCoordinator,
        entry: ConfigEntry,
        zone_id: int,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self.entry = entry
        self._zone_id = zone_id
        
        # Unique ID uses the user's email to avoid collisions across multiple accounts
        email = entry.title.lower()
        self._attr_unique_id = f"{email}_zone_{zone_id}"
        self._attr_name = coordinator.api.zone_labels.get(zone_id, f"Zone {zone_id}")

        # Set up device info to group all entities under one Climote device
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
        return self.coordinator.data.get(key, {})

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature (thermostat setting)."""
        try:
            thermostat = self._zone_data.get("thermostat")
            # Zones without a physical sensor report "00" or 0 — treat as unknown
            if thermostat is not None and float(thermostat) > 0:
                return float(thermostat)
        except (ValueError, TypeError):
            pass
        return None

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature inside the zone."""
        try:
            temp = self._zone_data.get("temperature")
            # Some zones (like Hot Water) might return '00' or 0 for temperature
            if temp is not None and float(temp) > 0:
                return float(temp)
        except (ValueError, TypeError):
            pass
        return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return current HVAC mode."""
        # status "5" = Boost is active
        # status "1" = Schedule/timer is running (heating)
        # status None or "0" = idle / off
        status = self._zone_data.get("status")
        if status in ("5", "1"):
            return HVACMode.HEAT
        return HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available HVAC modes."""
        return [HVACMode.HEAT, HVACMode.OFF]

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action (HEATING or IDLE)."""
        status = self._zone_data.get("status")
        # "5" = Boost, "1" = Schedule-driven heat
        if status in ("5", "1"):
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return 5.0

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return 32.0

    @property
    def target_temperature_step(self) -> float:
        """Return the target temperature step."""
        return 0.5

    def _get_zone_boost_duration(self) -> float:
        """Read the per-zone boost duration from the shared number entity store."""
        durations = self.hass.data[DOMAIN][self.entry.entry_id].get("boost_durations", {})
        return float(durations.get(self._zone_id, DEFAULT_BOOST_DURATION))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode. Setting HEAT triggers boost, OFF stops boost."""
        if hvac_mode == HVACMode.HEAT:
            duration = self._get_zone_boost_duration()
            LOGGER.info("Setting HVAC Mode to HEAT for zone %d (boosting %s hours)", self._zone_id, duration)
            success = await self.coordinator.api.set_boost(self._zone_id, duration)
            if success:
                await self.coordinator.async_force_gsm_refresh()
        elif hvac_mode == HVACMode.OFF:
            LOGGER.info("Setting HVAC Mode to OFF for zone %d (cancelling boost)", self._zone_id)
            success = await self.coordinator.api.cancel_boost(self._zone_id)
            if success:
                await self.coordinator.async_force_gsm_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature. Climote schedules are template-based, so setting temperature triggers a boost."""
        temperature = kwargs.get("temperature")
        if temperature is None:
            return

        LOGGER.warning(
            "Setting arbitrary target temperature directly on Climote is template-bound. "
            "Triggering a temporary Boost instead to heat zone %d to target.",
            self._zone_id
        )
        duration = self._get_zone_boost_duration()
        success = await self.coordinator.api.set_boost(self._zone_id, duration)
        if success:
            await self.coordinator.async_force_gsm_refresh()
