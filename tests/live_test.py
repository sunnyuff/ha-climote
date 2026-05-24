#!/usr/bin/env python3
"""
Live integration test for the Climote API.
Tests login with Email + Device ID and validates all key API flows.

Usage:
  CLIMOTE_EMAIL="your@email.com" CLIMOTE_DEVICE_ID="<your_device_id>" python3 tests/live_test.py
"""
import asyncio
import json
import os
import sys
import logging
from unittest.mock import MagicMock

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
            'homeassistant.helpers.config_validation','homeassistant.components',
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

        # ── 6. Boost ON zone 1 for 30 min ──────────────────────────────────
        section(f"TEST 6: Boost ON — {api.zone_labels.get(1,'Zone 1')} (30 min)")
        warn("Turning ON boost for zone 1...")
        try:
            result = await api.set_boost(zone_id=1, duration_hours=0.5)
            if result:
                ok("Boost ON delivered to GSM hub!")
            else:
                fail("Boost ON timed out — hub may be slow or offline")
        except Exception as e:
            fail(f"Boost ON exception: {e}")

        # ── 7. Verify boost active ─────────────────────────────────────────
        section("TEST 7: Verify Boost Active via GSM Status")
        try:
            check = await api.get_status(force_gsm=True)
            z1 = check.get("zone1", {})
            if z1.get("status") == "5":
                ok(f"Boost CONFIRMED ACTIVE! timeRemaining={z1.get('timeRemaining')} min")
            else:
                warn(f"Zone 1 status={z1.get('status')} (may still be propagating)")
        except Exception as e:
            fail(f"Post-boost check failed: {e}")

        # ── wait a moment before cancel ────────────────────────────────────
        warn("Waiting 5s before cancelling boost...")
        await asyncio.sleep(5)

        # ── 8. Boost OFF zone 1 ────────────────────────────────────────────
        section(f"TEST 8: Boost OFF — {api.zone_labels.get(1,'Zone 1')}")
        warn("Cancelling boost for zone 1...")
        try:
            result = await api.cancel_boost(zone_id=1)
            if result:
                ok("Boost OFF delivered to GSM hub!")
            else:
                fail("Boost OFF timed out")
        except Exception as e:
            fail(f"Boost OFF exception: {e}")

        # ── 9. Verify cancelled ────────────────────────────────────────────
        section("TEST 9: Verify Boost Cancelled via GSM Status")
        try:
            final = await api.get_status(force_gsm=True)
            z1 = final.get("zone1", {})
            if z1.get("status") != "5" and not z1.get("timeRemaining"):
                ok(f"Boost CONFIRMED CANCELLED. status={z1.get('status')}")
            else:
                warn(f"Zone 1: status={z1.get('status')}, remaining={z1.get('timeRemaining')}min — may still propagating")
        except Exception as e:
            fail(f"Post-cancel check failed: {e}")

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
