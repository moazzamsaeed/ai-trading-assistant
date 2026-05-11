"""Risk manager — pure Python, no LLM in the loop.

Enforces the hard constraints documented in `docs/DECISIONS.md` D-001 and D-007.
The LLM proposes; the risk manager disposes.
"""

from __future__ import annotations


class RiskRejectionError(Exception):
    """Raised when an agent signal violates a hard risk constraint."""


def validate_account_is_cash() -> None:
    """Refuse to start if the live Alpaca account is anything other than cash.

    Called once at Hermes startup. Hermes will not enter its main loop unless
    this passes. Margin and leverage are forbidden (D-001).
    """
    raise NotImplementedError("Phase 1 — to be implemented")


def validate_signal(signal: object) -> None:
    """Run a proposed trade signal through every hard check.

    Checks (Phase 1+):
      - account is cash (re-checked, not trusted from startup cache)
      - cash available >= order notional
      - no naked options structure
      - daily loss limit not breached
      - max concurrent positions not exceeded
      - max position size not exceeded

    Raises RiskRejection on any failure.
    """
    raise NotImplementedError("Phase 1 — to be implemented")


def kill_all_positions() -> None:
    """Emergency flatten — cancel orders, close positions, halt trading.

    Triggered by Discord `/kill` command or by daily loss limit breach.
    """
    raise NotImplementedError("Phase 1 — to be implemented")
