"""Scheduler registration tests."""

from __future__ import annotations

from trademaster import scheduler as sch


async def _noop_poster(_text: str) -> None:
    return None


def test_make_scheduler_registers_premarket_job():
    scheduler = sch.make_scheduler(_noop_poster)
    job = scheduler.get_job("premarket_briefing")
    assert job is not None

    trigger = job.trigger
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["day_of_week"] == "mon-fri"
    assert fields["hour"] == "8"
    assert fields["minute"] == "0"
    assert str(trigger.timezone) == sch.PREMARKET_TZ


async def test_run_premarket_once_invokes_poster(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def fake_briefing(**_kwargs):
        return "briefing body", object()

    monkeypatch.setattr(sch, "run_premarket_briefing", fake_briefing)

    await sch.run_premarket_once(poster)
    assert posted == ["briefing body"]


async def test_premarket_job_swallows_exception(monkeypatch):
    """A failing briefing must not propagate — scheduler keeps running."""
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def boom(**_kwargs):
        raise RuntimeError("router down")

    monkeypatch.setattr(sch, "run_premarket_briefing", boom)

    # Should not raise.
    await sch._premarket_job(poster)
    assert len(posted) == 1
    assert "failed" in posted[0].lower()
    assert "router down" in posted[0]
