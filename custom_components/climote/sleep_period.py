"""The daily window in which scheduled polls are skipped, to save SMS. No Home Assistant imports."""
from datetime import time


def parse_time(value: str | None) -> time:
    """Parse "HH:MM" or "HH:MM:SS"; anything missing or malformed is midnight."""
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        return time(0, 0)


def in_sleep_period(now: time, start: time, end: time) -> bool:
    """True from `start` up to, not including, `end`, which may be past midnight. Equal times: never."""
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end
