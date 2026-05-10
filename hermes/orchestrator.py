"""Hermes orchestrator entry point.

Responsibilities:
- Receive scheduled events from `scheduler`
- Route work to sub-agents via `router`
- Pass agent signals through `risk_manager` before any execution
- Persist signals/trades/agent_runs to SQLite
- Notify Discord on alerts, trade actions, and errors
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError("Phase 1 — to be implemented")


if __name__ == "__main__":
    main()
