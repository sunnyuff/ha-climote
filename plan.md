# Climote Home Assistant Integration Plan

This document outlines the architecture, file structures, components, and verification plans executed to build the Climote integration.

---

## 🏗️ Architecture Design

```
                     ┌────────────────────────┐
                     │ Home Assistant Core    │
                     │  (on Raspberry Pi)     │
                     └───────────┬────────────┘
                                 │
                     ┌───────────▼────────────┐
                     │  DataUpdateCoordinator │
                     │   (Standard Polls      │
                     │    every 30 mins)      │
                     └───────────┬────────────┘
                                 │
                     ┌───────────▼────────────┐
                     │ ClimoteAPI Client      │
                     │  (Authenticates,       │
                     │   resolves cookies)    │
                     └───────────┬────────────┘
                                 │ HTTPS (POST / GET)
                     ┌───────────▼────────────┐
                     │ climote.climote.ie     │
                     │  (Climote Cloud)       │
                     └───────────┬────────────┘
                                 │ GSM / SMS SMS Command
                     ┌───────────▼────────────┐
                     │ Physical Boiler Hub    │
                     │  (GSM Hub at Home)     │
                     └────────────────────────┘
```

---

## 📂 Custom Component File Layout

All files are placed under `/custom_components/climote/`:

| File | Purpose |
|------|---------|
| `manifest.json` | Specifies custom component metadata, dependencies, and version. |
| `const.py` | Declares logging handlers, global constants, URL endpoints, and config keys. |
| `strings.json` | Outlines translated configuration input forms for Home Assistant user setup. |
| `translations/en.json` | Contains English string localizations. |
| `api.py` | Contains the `ClimoteAPI` class that performs logins, CSRF token extraction, escaped JSON decodings, and GSM wait pollings. |
| `coordinator.py` | Inherits from `DataUpdateCoordinator` to manage background status fetches and direct GSM force updates. |
| `config_flow.py` | Sets up credential checks, optional PIN prompts, and custom options intervals. |
| `__init__.py` | Initializes standard platform setups and manages reloading listeners. |
| `climate.py` | Renders a unified `ClimateEntity` for each zone, supporting target reads and boost HEAT triggers. |
| `switch.py` | Adds tactile ON/OFF Boost buttons on your home dashboards. |
| `sensor.py` | Displays high-resolution room temperatures and countdown clocks for remaining boost minutes. |

---

## 🧪 Verification & Local Tests

We created a custom mock suite using `pytest-asyncio` inside `tests/test_climote.py`.

### Test Cases Covered:
1. **`test_pre_login_security_check_pin_not_required`**: Verifies that standard credentials are submit correctly when no passcode/PIN is needed.
2. **`test_login_invalid_auth`**: Confirms that login failure redirects throw a `ClimoteAuthError` immediately.
3. **`test_get_status_cached_double_json`**: Checks that double JSON-escaped strings from the cached `get-status` endpoint are decoded safely.
4. **`test_get_status_gsm_wait_loop`**: Validates the async loop checking `/waiting-get-status-response` until it succeeds or times out.
5. **`test_set_boost_success`**: Confirms that boost triggers poll the command delivery reports until they return `OK`.

### Test Executions:
```bash
python3 -m pytest tests/test_climote.py
```
**Results**:
```
tests/test_climote.py .....                                              [100%]
============================== 5 passed in 0.06s ===============================
```
All integration and logic tests successfully pass.
