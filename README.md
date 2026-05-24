# ha-climote — Climote Heating for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/sunnyuff/ha-climote.svg)](https://github.com/sunnyuff/ha-climote/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A modern, fully-async Home Assistant custom integration for the **Climote GSM Home Heating Controller** — reverse-engineered from the official Climote web portal at `climote.climote.ie`.

> **Live Tested** — Every API flow (login, status, boost on/off) verified against a real Climote GSM hub. 9/9 tests pass.

---

## ✨ Features

| Entity | Type | Description |
|---|---|---|
| **Living / Bed / Water Boost** | Switch | Toggle boost on/off for each zone |
| **Living / Bed / Water** | Climate | Full thermostat card with HVAC mode |
| **Living Temperature** | Sensor | Real-time temperature (°C) from hub sensor |
| **Boost Time Remaining** | Sensor | Minutes left on active boost |

- 🔑 Secure credential entry — Device No. is masked as a password field
- 🔄 Automatic zone name discovery (reads "Living", "Bed", "Water" from your hub)
- ⚡ Smart polling — cached cloud reads protect your GSM SMS limits
- 🔁 Re-authentication flow — seamlessly handles session expiry
- ⚙️ Configurable boost duration (0.5 – 8 hours) and poll interval via Options

---

## 📦 Installation

### Via HACS (Recommended)

1. Open **HACS** in Home Assistant
2. Click **Integrations** → top-right **⋮ menu** → **Custom repositories**
3. Add: `https://github.com/sunnyuff/ha-climote` with category **Integration**
4. Find **Climote Heating** and click **Install**
5. Restart Home Assistant

### Manual Install

```bash
# From your ha-climote repo directory:
scp -r custom_components/climote pi@<your-ha-ip>:/config/custom_components/
```
Then restart Home Assistant.

---

## ⚙️ Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Climote Heating**
3. Enter your credentials:

   | Field | What to enter |
   |---|---|
   | **Email Address** | Your Climote portal login email |
   | **Climote Device No.** | The 10-digit number on your hub (e.g. `1234567890`) |
   | **Security PIN** | Only if your account has a PIN enabled (leave blank otherwise) |

4. Click **Submit** — your zones (`Living`, `Bed`, `Water`) appear automatically!

> **Where is my Climote Device No.?**
> It's printed on the label of your physical Climote hub, and also shown as the second field on the [Climote login page](https://climote.climote.ie/manager/login).

---

## 🎛️ Options

After setup, click **Configure** on the integration to adjust:

| Option | Default | Description |
|---|---|---|
| **Polling Interval** | 1800s (30 min) | How often to refresh cached cloud status |
| **Default Boost Duration** | 1 hour | Duration used when toggling boost via switch/climate |

> ⚠️ **Do not set polling below 1800s (30 min).** Climote uses GSM/SMS to communicate with the hub. Excessive polling will exhaust your SMS credit.

---

## 🏗️ Architecture

```
Home Assistant
└── ClimoteDataUpdateCoordinator   ← polls cached cloud state every 30 min
    └── ClimoteAPI
        ├── login()                ← Email + Device No. → PHPSESSID cookie
        ├── get_status(force=0)    ← fast cached read, no SMS
        ├── get_status(force=1)    ← triggers real GSM hub poll via SMS
        ├── set_boost(zone, hours) ← POST /manager/boost, waits for DELIVERED
        └── cancel_boost(zone)     ← POST /manager/boost with "stop"

Entities (per zone: Living, Bed, Water)
├── climate.climote_living         ← HVAC card, set mode = boost on/off
├── switch.climote_living_boost    ← simple on/off toggle
└── sensor.climote_living_*        ← temperature + boost time remaining
```

---

## 📖 Climote Web Portal API Reference

The full reverse-engineered API is documented below. Use this with Postman or curl.

> [!WARNING]
> Every endpoint under `/manager/` requires the `PHPSESSID` session cookie from login.

### Base URL
```
https://climote.climote.ie
```

---

### 1. Pre-Login Security PIN Check

* **URL**: `/index/check-user-security`
* **Method**: `POST`
* **Payload**: `email=<USER_EMAIL>`
* **Response**: `0` (no PIN needed) or `1` (PIN required)

---

### 2. Login

* **URL**: `/manager/login`
* **Method**: `POST`
* **Headers**: `Referer: https://climote.climote.ie/manager/login`

> [!IMPORTANT]
> **Counter-Intuitive HTML Field Mappings** (confirmed via live traffic capture):
> - HTML field `password` → your **Email Address**
> - HTML field `username` → your **Climote Device No.** (e.g. `1234567890`)
> - HTML field `passcode` → **Security PIN** (blank if not needed)
> - HTML field `loginBtn` → literal string `Log in`

```
password=<USER_EMAIL>&username=<CLIMOTE_DEVICE_NO>&passcode=<PIN>&loginBtn=Log+in
```

* **Response**: `302 Found` → sets `PHPSESSID` cookie in `Set-Cookie` header.

---

### 3. CSRF Token & Schedule ID

* **URL**: `/manager/index` — `GET`
* **Extract CSRF**: `name="cs_token_rf"\s+value="([^"]+)"`
* **Extract Schedule ID**: `heatingScheduleId=(\d+)`

---

### 4. Status — Cached (no SMS)

* **URL**: `/manager/get-status?force=0` — `GET`
* **Header**: `X-Requested-With: XMLHttpRequest`
* **Response**: Double-JSON encoded string — parse twice.

---

### 5. Status — Real-time GSM Poll

* **URL**: `/manager/get-status?force=1` — triggers SMS to hub
* Then poll: `/manager/waiting-get-status-response` every 3s until not `"0"`
* **Status field**: `"5"` = boost active, `null` = idle

---

### 6. Boost Control

* **URL**: `/manager/boost` — `POST`
* **Payload**:
  ```
  zoneIds[1]=<VAL>&zoneIds[2]=nochange&zoneIds[3]=nochange&cs_token_rf=<TOKEN>
  ```
* **Values**: `nochange` | `stop` | `0.5` | `1` | `2` | `3` (hours)
* **Then poll**: `/manager/wait-for-delivery-report` every 3s until `DELIVERED`

---

### 7. Zone Labels

* **URL**: `/manager/get-heating-schedule?heatingScheduleId=<ID>&startday=sunday`
* **Response**: XML with `<zone id="1"><label>Living</label></zone>`

---

## 🤝 Contributing

PRs welcome! Please open an issue first for major changes.

## 📄 License

[MIT](LICENSE) © 2026 Sunny Jhunjhunwala
