#!/usr/bin/env python3
"""
Live integration test for the Climote API.
Tests login with Email + Device ID and validates all key API flows.

Usage:
  Put CLIMOTE_EMAIL, CLIMOTE_DEVICE_ID and (if needed) CLIMOTE_PIN in a gitignored `.env`
  file in the repo root (KEY=value per line), or export them, then:
      python3 tests/live_test.py            # read-only checks plus set-temperature (restores it)
      python3 tests/live_test.py --boost    # also run the boost on/off tests (off by default)
      python3 tests/live_test.py --boost-only   # login, then only the boost tests (no regression)
  Set CLIMOTE_TEST_ZONE (default 1) to pick the zone for set-temperature.
  Set CLIMOTE_BOOST_ZONE (default 3, hot water) to pick the zone for the boost tests. They boost it
  for 15 minutes and cancel if the hub reports it. Each command and each status read costs an SMS
  (3 at best, 8 at worst); the zone really heats while it runs.
"""
import asyncio
import json
import os
import sys
import logging
import time
from unittest.mock import MagicMock

def _load_dotenv():
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


_load_dotenv()
BOOST_ONLY = "--boost-only" in sys.argv
RUN_BOOST = "--boost" in sys.argv or BOOST_ONLY
BOOST_ZONE = int(os.environ.get("CLIMOTE_BOOST_ZONE", "3"))
ZONE = int(os.environ.get("CLIMOTE_TEST_ZONE", "1"))

EMAIL = os.environ.get("CLIMOTE_EMAIL", "")
DEVICE_ID = os.environ.get("CLIMOTE_DEVICE_ID", "")
PIN = os.environ.get("CLIMOTE_PIN", "")

if not EMAIL or not DEVICE_ID:
    print("ERROR: Set CLIMOTE_EMAIL and CLIMOTE_DEVICE_ID environment variables.")
    sys.exit(1)

# Stub homeassistant so we can import the real integration code
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for mod in ['homeassistant','homeassistant.config_entries','homeassistant.const',
            'homeassistant.core','homeassistant.helpers','homeassistant.helpers.update_coordinator',
            'homeassistant.helpers.entity_platform','homeassistant.helpers.aiohttp_client',
            'homeassistant.helpers.config_validation','homeassistant.util','homeassistant.helpers.event','homeassistant.exceptions','homeassistant.components',
            'homeassistant.components.climate','homeassistant.components.switch',
            'homeassistant.components.sensor']:
    sys.modules.setdefault(mod, MagicMock())

import aiohttp
from custom_components.climote.api import ClimoteAPI, ClimoteAuthError, ClimoteConnectionError

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("live_test")

results = []

def ok(msg):
    results.append((True, msg))
    print(f"✅  {msg}")

def fail(msg):
    results.append((False, msg))
    print(f"❌  {msg}")

def warn(msg):
    print(f"⚠️   {msg}")

def section(title):
    print(f"\n{'═'*60}\n{title}\n{'═'*60}")


async def run_regression(api):
    """Read-only checks plus set-temperature (restores the setpoint)."""
    # ── 3. Zone Labels ─────────────────────────────────────────────────
    section("TEST 3: Zone Labels from XML Schedule")
    defaults = {1: "Zone 1", 2: "Zone 2", 3: "Zone 3"}
    if api.zone_labels and api.zone_labels != defaults:
        ok(f"Custom zone labels: {api.zone_labels}")
    elif api.zone_labels:
        warn(f"Using default labels (no custom names found): {api.zone_labels}")
    else:
        fail("Zone labels empty")

    # ── 4. Cached Status (force=0) ─────────────────────────────────────
    section("TEST 4: Cached Status Poll (force=0, no GSM)")
    try:
        status = await api.get_status(force_gsm=False)
        if status and "zone1" in status:
            ok("Cached status parsed successfully")
            print(f"   unit_time : {status.get('unit_time')}")
            print(f"   updated_at: {status.get('updated_at')}")
            for z in [1, 2, 3]:
                zd = status.get(f"zone{z}", {})
                lbl = api.zone_labels.get(z, f"Zone {z}")
                print(f"   zone{z} ({lbl:<8}): temp={zd.get('temperature')}°C  "
                      f"thermostat={zd.get('thermostat')}°C  "
                      f"status={zd.get('status')}  "
                      f"timeRemaining={zd.get('timeRemaining')} min")
        else:
            fail(f"Bad status structure: {status}")
    except Exception as e:
        fail(f"Cached status failed: {e}")

    # ── 5. Forced GSM Status (force=1) ─────────────────────────────────
    section("TEST 5: Force Real-Time GSM Hub Status (takes ~15s)")
    warn("Sending SMS to hub — please wait...")
    try:
        gsm = await api.get_status(force_gsm=True)
        if gsm and "zone1" in gsm:
            ok("GSM status received!")
            print(f"   unit_time : {gsm.get('unit_time')}")
            print(f"   updated_at: {gsm.get('updated_at')}")
            for z in [1, 2, 3]:
                zd = gsm.get(f"zone{z}", {})
                lbl = api.zone_labels.get(z, f"Zone {z}")
                print(f"   zone{z} ({lbl:<8}): temp={zd.get('temperature')}°C  "
                      f"thermostat={zd.get('thermostat')}°C  "
                      f"status={zd.get('status')}  "
                      f"timeRemaining={zd.get('timeRemaining')} min")
        else:
            fail(f"Unexpected GSM response: {gsm}")
    except Exception as e:
        fail(f"GSM status failed: {e}")

    # ── 6. Set temperature (restores the original setpoint) ────────────
    section(f"TEST 6: Set temperature — zone {ZONE}")
    try:
        print(f"   zones on the portal's temperature form: {await api.temperature_form_zones()}")
        before = (await api.get_status(force_gsm=True)).get(f"zone{ZONE}", {})
        orig = float(before.get("thermostat") or 0)
        print(f"   raw zone{ZONE} before: {before}")
        if orig < 5:
            warn("No valid setpoint to restore; skipping")
        else:
            target = orig + 0.5 if orig + 0.5 <= 32 else orig - 0.5
            if await api.set_temperature(ZONE, target):
                ok(f"POST accepted for {target}")
            else:
                fail("Set temperature POST rejected")
            after = {}
            for i in range(1, 7):  # the delivery report never completes, so poll the hub
                await asyncio.sleep(10)
                after = (await api.get_status(force_gsm=True)).get(f"zone{ZONE}", {})
                print(f"   try {i}: thermostat={after.get('thermostat')}  raw={after}")
                if float(after.get("thermostat") or 0) == target:
                    break
            if float(after.get("thermostat") or 0) == target:
                ok(f"Hub reports setpoint {target}")
            else:
                fail(f"Hub still reports {after.get('thermostat')} after setting {target}")
            await api.set_temperature(ZONE, orig)
            restored = (await api.get_status(force_gsm=True)).get(f"zone{ZONE}", {})
            print(f"   restored to {orig}: hub says {restored.get('thermostat')}")
    except Exception as e:
        fail(f"Set temperature exception: {e}")


async def run_boost(api):
    """Boost BOOST_ZONE for 15 minutes and check the hub reports it, spending as few SMS as possible.

    What this proves about the fix: set_boost/cancel_boost return as soon as the command is POSTed
    (they used to wait 90 s for a delivery report that never comes, then report failure, so the HA
    switch never refreshed), and the hub's status then reads as boost (5, number or string).

    SMS budget (each command and each forced hub read is one):
      before: 0 (cached read)  ON: 1 command + up to 3 reads  OFF: 1 command + up to 2 reads
    so 3 at best and 8 at worst. If the boost is never seen, cancel is skipped (2 to 3 SMS saved):
    the boost ends itself after 15 minutes. Reads are 60 s apart because the hub is slow and a
    read that overlaps its own SMS only makes it slower.
    """
    zone = BOOST_ZONE
    label = api.zone_labels.get(zone, f"Zone {zone}")
    sms = {"commands": 0}
    gap = 60

    async def zone_now(force):
        return (await api.get_status(force_gsm=force)).get(f"zone{zone}", {})

    def boosting(z):
        return str(z.get("status")) == "5"

    async def wait_for(done, what, tries):
        z = {}
        for i in range(1, tries + 1):
            print(f"   waiting {gap}s before read {i}/{tries} (one SMS)...")
            await asyncio.sleep(gap)
            z = await zone_now(True)
            print(f"   read {i}: status={z.get('status')!r} timeRemaining={z.get('timeRemaining')} burner={z.get('burner')}  raw={z}")
            if done(z):
                return True
        fail(f"{what}: not seen after {tries} reads (last raw: {z})")
        return False

    reads_at_start = api.gsm_reads
    boost_seen = False
    section(f"BOOST: before — {label} (zone {zone}), cached read, no forced SMS")
    before = await zone_now(False)
    print(f"   raw zone{zone}: {before}  (status is a {type(before.get('status')).__name__})")
    if boosting(before):
        fail(f"Zone {zone} already shows boost; cancel it first so the test means something")
        return

    section(f"BOOST: ON — {label} (15 min)")
    try:
        t0 = time.monotonic()
        result = await api.set_boost(zone_id=zone, duration_hours=0.25)
        took = time.monotonic() - t0
        sms["commands"] += 1
        print(f"   set_boost returned {result} after {took:.1f}s")
        if result:
            ok("set_boost returned True")
        else:
            fail("set_boost returned False (the POST failed)")
        if took < 15:
            ok(f"set_boost returned promptly ({took:.1f}s; the old delivery wait took 90s)")
        else:
            fail(f"set_boost took {took:.1f}s: it is still waiting on something")
        boost_seen = result and await wait_for(boosting, "Hub reporting boost", tries=3)
        if boost_seen:
            ok("Hub reports boost (status 5)")
    except Exception as e:
        fail(f"Boost ON exception: {e}")

    if not boost_seen:
        warn(f"Boost not confirmed, so not cancelling (saves SMS). If it started it ends by itself within 15 min; "
             f"check the portal or the boiler, or cancel zone {zone} by hand.")
    else:
        section(f"BOOST: OFF — {label}")
        try:
            t0 = time.monotonic()
            result = await api.cancel_boost(zone_id=zone)
            took = time.monotonic() - t0
            sms["commands"] += 1
            print(f"   cancel_boost returned {result} after {took:.1f}s")
            if result and took < 15:
                ok(f"cancel_boost returned True promptly ({took:.1f}s)")
            else:
                fail(f"cancel_boost returned {result} after {took:.1f}s")
            if await wait_for(lambda z: not boosting(z) and not z.get("timeRemaining"), "Boost cleared", tries=2):
                ok("Hub reports the boost cleared")
        except Exception as e:
            fail(f"Boost OFF exception: {e} — cancel zone {zone} by hand in the portal")

    reads = api.gsm_reads - reads_at_start
    print(f"\n   SMS used by the boost tests: {sms['commands']} commands + {reads} hub reads = {sms['commands'] + reads}")


async def run():
    jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        api = ClimoteAPI(session, EMAIL, DEVICE_ID, PIN)

        # ── 1. Login ───────────────────────────────────────────────────────
        section("TEST 1: Login with Email + Climote Device No.")
        try:
            success = await api.login()
            if success and api._csrf_token:
                ok(f"Login OK. CSRF token: {api._csrf_token[:16]}...")
            else:
                fail("Login returned True but no CSRF token extracted")
                return
        except ClimoteAuthError as e:
            fail(f"Auth error: {e}")
            return
        except ClimoteConnectionError as e:
            fail(f"Connection error: {e}")
            return

        # ── 2. Schedule ID ─────────────────────────────────────────────────
        section("TEST 2: Schedule ID Auto-Discovery")
        if api.schedule_id:
            ok(f"Schedule ID: {api.schedule_id}")
        else:
            fail("Could not discover heatingScheduleId")

        if not BOOST_ONLY:
            await run_regression(api)

        if RUN_BOOST:
            await run_boost(api)
        else:
            warn("Skipping boost tests (pass --boost to run them)")

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("LIVE TEST SUMMARY")
    print(f"{'═'*60}")
    passed = sum(1 for ok_, _ in results if ok_)
    failed = sum(1 for ok_, _ in results if not ok_)
    for ok_, msg in results:
        print(f"  {'✅' if ok_ else '❌'}  {msg}")
    print(f"\n  {passed}/{len(results)} passed, {failed} failed")
    if failed == 0:
        print("\n🎉  All tests passed — integration is production-ready!\n")
    else:
        print("\n⚠️   Some tests failed — review above.\n")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
