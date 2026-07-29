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
    # Global master pause — a manual/catastrophic kill-switch that halts every
    # strategy. Checked by all trading jobs.
    paused_until: datetime | None = None
    # Directional-only pause — set when the directional engine trips its own loss
    # limit off its isolated $10k pool. Does NOT stop the iron condor (separate
    # pools; see get_effective_capital(strategy_group="directional")).
    directional_paused_until: datetime | None = None
    last_kill_at: datetime | None = None

    def is_paused(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.paused_until is not None and now < self.paused_until

    def pause(self, *, hours: float = 0, minutes: float = 0) -> None:
        """Global pause — halts all strategies for the given duration."""
        self.paused_until = datetime.now(UTC) + timedelta(hours=hours, minutes=minutes)

    def is_directional_paused(self, now: datetime | None = None) -> bool:
        """True if directional is halted by EITHER the global or directional pause."""
        now = now or datetime.now(UTC)
        if self.is_paused(now):
            return True
        return self.directional_paused_until is not None and now < self.directional_paused_until

    def pause_directional(self, *, hours: float = 0, minutes: float = 0) -> None:
        """Pause the directional engine only (the condor keeps trading)."""
        self.directional_paused_until = datetime.now(UTC) + timedelta(hours=hours, minutes=minutes)


_state = SystemState()


def get_state() -> SystemState:
    return _state


def reset_state_for_tests() -> None:
    """Reset to defaults. Only for use in pytest fixtures."""
    global _state
    _state = SystemState()
