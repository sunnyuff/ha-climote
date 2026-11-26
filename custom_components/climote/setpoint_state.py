"""The temperature a command asked for, shown until a read of the hub settles it.

Setting a temperature is an SMS to the hub, and the hub needs a while to act on it. So the entity shows
the requested value at once, and only a read made after the settle time may overrule it.
Kept free of Home Assistant imports so it can be tested on its own.
"""
import time
from typing import Callable

# How long after a command a status read can still be expected to show the old temperature.
SETPOINT_SETTLE_SECONDS = 180
# A timer may fire a moment early by this module's clock.
_TIMER_SLACK_SECONDS = 5


class SetpointExpectations:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[int, tuple[float, float]] = {}  # zone -> (temperature, settled from)

    def expect(self, zone_id: int, temperature: float) -> None:
        self._items[zone_id] = (temperature, self._clock() + SETPOINT_SETTLE_SECONDS - _TIMER_SLACK_SECONDS)

    def get(self, zone_id: int) -> float | None:
        item = self._items.get(zone_id)
        return None if item is None else item[0]

    def reconcile(self, data: dict, fresh: bool) -> tuple[list[int], list[tuple[int, float, object]]]:
        """Compare a status read with each expectation.

        `fresh` is False when the hub did not answer and `data` is the portal's cached copy.
        Returns (zones that read as requested, (zone, requested, reported) triples the hub overruled).
        Both are forgotten. A mismatch in a stale or too early read is ignored.
        """
        confirmed, overruled = [], []
        for zone_id, (wanted, settled_from) in list(self._items.items()):
            zone = data.get(f"zone{zone_id}") if isinstance(data, dict) else None
            if not zone:
                continue
            reported = zone.get("thermostat")
            try:
                matches = float(reported) == wanted
            except (TypeError, ValueError):
                matches = False
            if matches:
                confirmed.append(zone_id)
            elif fresh and self._clock() >= settled_from:
                overruled.append((zone_id, wanted, reported))
            else:
                continue
            del self._items[zone_id]
        return confirmed, overruled
