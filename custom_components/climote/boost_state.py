"""What a boost command asked for, believed until the hub confirms it or the boost would have ended.

A boost or cancel is an SMS to the hub, and the hub needs a while to act on it: a status read a
second later still returns the old state. So the entities show the requested state in the meantime.
Kept free of Home Assistant imports so it can be tested on its own.
"""
import time
from typing import Callable

# Status value the hub reports while a zone is boosting. It may arrive as a number or a string.
BOOST_STATUS = "5"


def is_boosting(zone: dict) -> bool:
    return str(zone.get("status")) == BOOST_STATUS


class BoostExpectations:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[int, tuple[bool, float]] = {}  # zone -> (boost on?, believe until)

    def expect(self, zone_id: int, on: bool, ttl_seconds: float) -> None:
        self._items[zone_id] = (on, self._clock() + ttl_seconds)

    def get(self, zone_id: int) -> bool | None:
        """The requested state, or None once it is confirmed, has lapsed or was never set."""
        item = self._items.get(zone_id)
        if item is None:
            return None
        on, until = item
        if self._clock() >= until:
            del self._items[zone_id]
            return None
        return on

    def reconcile(self, data: dict) -> tuple[list[int], list[tuple[int, dict]]]:
        """Compare the hub's status with each expectation.

        Returns (zones the hub confirmed, which are forgotten; (zone, raw data) pairs it contradicts,
        which are kept).
        """
        confirmed, contradicted = [], []
        for zone_id, (on, _until) in list(self._items.items()):
            zone = data.get(f"zone{zone_id}", {}) if isinstance(data, dict) else {}
            if is_boosting(zone) == on:
                del self._items[zone_id]
                confirmed.append(zone_id)
            else:
                contradicted.append((zone_id, zone))
        return confirmed, contradicted
