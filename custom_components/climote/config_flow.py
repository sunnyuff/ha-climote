import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectOptionDict,
)
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    LOGGER,
    CONF_EMAIL,
    CONF_DEVICE_ID,
    CONF_PIN,
    CONF_POLL_INTERVAL,
    CONF_BOOST_DURATION,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_BOOST_DURATION,
    MIN_POLL_INTERVAL,
)
from .api import ClimoteAPI, ClimoteAuthError, ClimoteConnectionError

# Boost duration presets — 15-minute intervals, value stored as decimal hours
BOOST_DURATION_OPTIONS = [
    SelectOptionDict(value="0.5",  label="30 minutes"),
    SelectOptionDict(value="0.75", label="45 minutes"),
    SelectOptionDict(value="1.0",  label="1 hour"),
    SelectOptionDict(value="1.25", label="1 hour 15 minutes"),
    SelectOptionDict(value="1.5",  label="1 hour 30 minutes"),
    SelectOptionDict(value="1.75", label="1 hour 45 minutes"),
    SelectOptionDict(value="2.0",  label="2 hours"),
    SelectOptionDict(value="3.0",  label="3 hours"),
    SelectOptionDict(value="4.0",  label="4 hours"),
    SelectOptionDict(value="6.0",  label="6 hours"),
    SelectOptionDict(value="8.0",  label="8 hours"),
]

class ClimoteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Climote Heating.

    Step 1 (user): Collect Email + Device No. + optional PIN, validate login.
    Re-auth step: Allow the user to update credentials without removing the entry.
    """

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial setup step."""
        errors = {}

        if user_input is not None:
            # Use Device ID as the unique identifier — more stable than email
            await self.async_set_unique_id(user_input[CONF_DEVICE_ID].strip())
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            api = ClimoteAPI(
                session,
                email=user_input[CONF_EMAIL],
                device_id=user_input[CONF_DEVICE_ID],
                pin=user_input.get(CONF_PIN, ""),
            )

            try:
                await api.login()
            except ClimoteAuthError:
                errors["base"] = "invalid_auth"
            except ClimoteConnectionError:
                errors["base"] = "cannot_connect"
            except Exception as err:
                LOGGER.error("Unexpected error in Climote config flow: %s", err)
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="email")
                    ),
                    vol.Required(CONF_DEVICE_ID): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(CONF_PIN, default=""): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        """Handle re-authentication when the session expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Confirm re-authentication with updated credentials."""
        errors = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = ClimoteAPI(
                session,
                email=user_input[CONF_EMAIL],
                device_id=user_input[CONF_DEVICE_ID],
                pin=user_input.get(CONF_PIN, ""),
            )

            try:
                await api.login()
            except ClimoteAuthError:
                errors["base"] = "invalid_auth"
            except ClimoteConnectionError:
                errors["base"] = "cannot_connect"
            except Exception as err:
                LOGGER.error("Unexpected error in Climote re-auth: %s", err)
                errors["base"] = "unknown"
            else:
                # Update the existing entry with new credentials
                existing_entry = await self.async_set_unique_id(user_input[CONF_DEVICE_ID].strip())
                self.hass.config_entries.async_update_entry(
                    existing_entry, data=user_input
                )
                await self.hass.config_entries.async_reload(existing_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="email")
                    ),
                    vol.Required(CONF_DEVICE_ID): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(CONF_PIN, default=""): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this integration."""
        return ClimoteOptionsFlowHandler(config_entry)


class ClimoteOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Climote Heating."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the Climote options."""
        if user_input is not None:
            # Enforce minimum polling interval to protect GSM limits
            if user_input[CONF_POLL_INTERVAL] < MIN_POLL_INTERVAL:
                user_input[CONF_POLL_INTERVAL] = MIN_POLL_INTERVAL
            return self.async_create_entry(title="", data=user_input)

        poll_interval = self.config_entry.options.get(
            CONF_POLL_INTERVAL,
            self.config_entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        )
        boost_duration = self.config_entry.options.get(
            CONF_BOOST_DURATION,
            self.config_entry.data.get(CONF_BOOST_DURATION, DEFAULT_BOOST_DURATION),
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_POLL_INTERVAL, default=int(poll_interval)): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_POLL_INTERVAL,
                            max=86400,
                            step=60,
                            unit_of_measurement="seconds",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(CONF_BOOST_DURATION, default=str(float(boost_duration))): SelectSelector(
                        SelectSelectorConfig(
                            options=BOOST_DURATION_OPTIONS,
                            mode="list",
                        )
                    ),
                }
            ),
        )
