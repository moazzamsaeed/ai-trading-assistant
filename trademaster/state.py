"""In-process runtime state.

Phase 1.4b stores the pause flag and last kill timestamp in memory only.
Restart clears the pause. Phase 2 will move this to a `system_state` DB
table so state survives process restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class SystemState:
    paused_until: datetime | None = None
    last_kill_at: datetime | None = None

    def is_paused(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.paused_until is not None and now < self.paused_until

    def pause(self, *, hours: float = 0, minutes: float = 0) -> None:
        """Pause trading for the given duration starting now."""
        self.paused_until = datetime.now(UTC) + timedelta(hours=hours, minutes=minutes)


_state = SystemState()


def get_state() -> SystemState:
    return _state


def reset_state_for_tests() -> None:
    """Reset to defaults. Only for use in pytest fixtures."""
    global _state
    _state = SystemState()
