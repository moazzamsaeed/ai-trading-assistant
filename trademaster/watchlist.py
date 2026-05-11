"""User-managed watchlist persistence.

The pre-market research agent and the intraday scanner read this list at
the start of every run. Discord slash commands (/watchlist_add /watchlist_remove)
mutate it. Stored as a tiny JSON file under `data/` so changes survive
restarts and there's no DB migration overhead.

The iron-condor strategist is hard-coded to SPY (D-???) and ignores this
list — it's the SPY 0DTE iron condor strategy specifically.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from trademaster.logging import get_logger

log = get_logger(__name__)

WATCHLIST_PATH = Path("data/watchlist.json")

# Sensible default if the file is missing on first run.
DEFAULT_TICKERS: tuple[str, ...] = ("SPY", "QQQ", "IWM", "DIA")

# Ticker validation: 1–6 alphanumeric chars, optionally a `.` (e.g. BRK.B)
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,5}$")


def _normalize(ticker: str) -> str:
    return ticker.strip().upper()


def _validate(ticker: str) -> str:
    norm = _normalize(ticker)
    if not TICKER_RE.match(norm):
        raise ValueError(f"invalid ticker: {ticker!r}")
    return norm


# ----------------- file I/O -----------------


def _read(path: Path = WATCHLIST_PATH) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("watchlist_read_failed", path=str(path), error=str(e))
        return []
    if not isinstance(data, dict) or not isinstance(data.get("tickers"), list):
        log.warning("watchlist_bad_shape", path=str(path))
        return []
    return [_normalize(t) for t in data["tickers"] if isinstance(t, str)]


def _write(tickers: list[str], path: Path = WATCHLIST_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"tickers": tickers}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ----------------- public API -----------------


def load_tickers(path: Path = WATCHLIST_PATH) -> tuple[str, ...]:
    """Return current watchlist as an immutable tuple.

    Bootstraps from `DEFAULT_TICKERS` if the file is missing or unreadable.
    Always returns at least one ticker so downstream agents don't choke on
    an empty list (the user can explicitly clear via remove if they want).
    """
    tickers = _read(path)
    if not tickers:
        return DEFAULT_TICKERS
    return tuple(tickers)


def list_tickers(path: Path = WATCHLIST_PATH) -> list[str]:
    """Like load_tickers but mutable. Returns empty list if file is empty."""
    return _read(path)


def add_ticker(ticker: str, path: Path = WATCHLIST_PATH) -> tuple[list[str], bool]:
    """Add `ticker` to the watchlist. Returns (current_list, was_new).

    Validation: ticker must be 1–6 chars [A-Z][A-Z0-9.]*. Raises ValueError
    on malformed input. Duplicates are silently no-ops.
    """
    norm = _validate(ticker)
    existing = _read(path)
    if norm in existing:
        return existing, False
    existing.append(norm)
    _write(existing, path)
    log.info("watchlist_added", ticker=norm, total=len(existing))
    return existing, True


def remove_ticker(ticker: str, path: Path = WATCHLIST_PATH) -> tuple[list[str], bool]:
    """Remove `ticker`. Returns (current_list, was_present)."""
    norm = _validate(ticker)
    existing = _read(path)
    if norm not in existing:
        return existing, False
    existing.remove(norm)
    _write(existing, path)
    log.info("watchlist_removed", ticker=norm, total=len(existing))
    return existing, True


def seed(tickers: list[str], path: Path = WATCHLIST_PATH) -> list[str]:
    """Replace the entire watchlist with the validated, deduped input."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tickers:
        norm = _validate(t)
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    _write(out, path)
    log.info("watchlist_seeded", count=len(out))
    return out
