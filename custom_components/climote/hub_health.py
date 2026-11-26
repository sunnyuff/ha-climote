"""Whether the hub is still answering status reads. No Home Assistant imports.

The portal gives no "rate limited" answer; the sign is reads that the hub never replies to. After a few
in a row, reads are spaced far apart so they stop using up the SMS allowance. A hub that is switched
off or has no signal looks the same.
"""
import time
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum

# Unanswered reads in a row before the hub counts as silent.
SILENT_AFTER = 3
# While silent, the least time between test reads.
PROBE_SECONDS = 6 * 3600
# Poll timers can fire slightly early; without this a probe could slip by a whole poll interval.
_TIMER_SLACK_SECONDS = 300


class PollAction(Enum):
    SKIP = "skip"
    READ = "read"  # a plain poll
    TEST = "test"  # a forced hub read, to find out whether a silent hub is back


class HubHealth:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._clock = clock
        self._now = now
        self._last_timeout = 0.0
        self.timeouts = 0  # in a row
        self.last_reply: datetime | None = None
        self.on_change: Callable[[], None] | None = None

    @property
    def silent(self) -> bool:
        return self.timeouts >= SILENT_AFTER

    def may_read(self) -> bool:
        """False while the hub is silent and the next test read is not yet due."""
        return not self.silent or self._clock() - self._last_timeout >= PROBE_SECONDS - _TIMER_SLACK_SECONDS

    def poll_action(self, has_data: bool, asleep: bool) -> PollAction:
        """What a scheduled poll should do.

        A silent hub is left alone until a test is due, with or without data. The sleep period only
        holds once there is data to show (so HA starting at night still reads once). The test is forced
        because a plain poll may be answered from the portal's cache, which says nothing about the hub.
        """
        if not self.may_read() or (asleep and has_data):
            return PollAction.SKIP
        return PollAction.TEST if self.silent else PollAction.READ

    def reply(self) -> None:
        self.timeouts = 0
        self.last_reply = self._now()
        self._changed()

    def timeout(self) -> None:
        self.timeouts += 1
        self._last_timeout = self._clock()
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()
