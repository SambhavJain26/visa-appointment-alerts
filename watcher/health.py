from __future__ import annotations

import time


class SourceHealth:
    """Tracks whether a component is working; warns once when it has been failing, and once on recovery.

    Two modes:
    - default (sources that should succeed continuously, e.g. Telegram polling): failing when there
      has been no success for `fail_minutes`.
    - require_error=True (components that are legitimately idle, e.g. the AI check when no message
      needs checking): failing only after `min_failures` consecutive failures. A single hiccup
      after a long quiet period is NOT a failure.
    """

    def __init__(self, name: str, fail_minutes: int, require_error: bool = False,
                 min_failures: int = 3):
        self.name = name
        self.fail_seconds = fail_minutes * 60
        self.require_error = require_error
        self.min_failures = min_failures
        self.last_ok = time.time()  # grace period from startup
        self.last_error: str | None = None
        self.consecutive_failures = 0
        self.warned = False
        self.deferred_until = 0.0

    def defer_checks_until(self, deadline: float) -> None:
        """Do not call an intentional source cooldown an unexplained outage."""
        self.deferred_until = deadline

    def ok(self) -> None:
        self.last_ok = time.time()
        self.last_error = None
        self.consecutive_failures = 0

    def fail(self, error: str) -> None:
        self.last_error = error
        self.consecutive_failures += 1

    def is_failing(self) -> bool:
        if time.time() < self.deferred_until:
            return False
        if self.require_error:
            return self.consecutive_failures >= self.min_failures
        return (time.time() - max(self.last_ok, self.deferred_until)) > self.fail_seconds

    def check(self) -> str | None:
        """Return a message to send (warning or recovery) or None."""
        failing = self.is_failing()
        if failing and not self.warned:
            self.warned = True
            if self.require_error:
                what = f"has failed {self.consecutive_failures} times in a row"
            else:
                what = f"has been failing for {int((time.time() - max(self.last_ok, self.deferred_until)) // 60)} min"
            return f"⚠️ Slot watcher: {self.name} {what}.\nLast error: {self.last_error or 'unknown'}"
        if not failing and self.warned:
            self.warned = False
            return f"✅ Slot watcher: {self.name} is working again."
        return None
