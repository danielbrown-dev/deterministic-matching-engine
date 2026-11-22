from __future__ import annotations

import pytest

from dme.sim.scheduler import Scheduler, rng_stream


def test_actions_run_in_time_order_and_advance_the_clock() -> None:
    scheduler = Scheduler()
    seen: list[tuple[str, int]] = []
    scheduler.at(30, lambda: seen.append(("c", scheduler.now)))
    scheduler.at(10, lambda: seen.append(("a", scheduler.now)))
    scheduler.at(20, lambda: seen.append(("b", scheduler.now)))
    while scheduler.run_next():
        pass
    assert seen == [("a", 10), ("b", 20), ("c", 30)]


def test_ties_run_in_scheduling_order() -> None:
    scheduler = Scheduler()
    seen: list[int] = []
    for n in range(50):
        scheduler.at(5, lambda n=n: seen.append(n))  # type: ignore[misc]
    while scheduler.run_next():
        pass
    assert seen == list(range(50))


def test_actions_can_schedule_more_actions() -> None:
    scheduler = Scheduler()
    ticks: list[int] = []

    def tick() -> None:
        ticks.append(scheduler.now)
        if len(ticks) < 3:
            scheduler.after(7, tick)

    scheduler.after(7, tick)
    while scheduler.run_next():
        pass
    assert ticks == [7, 14, 21]


def test_cannot_schedule_in_the_past() -> None:
    scheduler = Scheduler()
    scheduler.at(10, lambda: None)
    scheduler.run_next()
    with pytest.raises(ValueError, match="before now"):
        scheduler.at(9, lambda: None)


def test_empty_scheduler_reports_nothing_to_run() -> None:
    assert Scheduler().run_next() is False


def test_rng_streams_are_reproducible_and_independent() -> None:
    def draws(seed: int, name: str) -> list[int]:
        rng = rng_stream(seed, name)
        return [rng.randrange(10**9) for _ in range(5)]

    assert draws(1, "maker/0") == draws(1, "maker/0")
    assert draws(1, "maker/0") != draws(1, "maker/1")
    assert draws(1, "maker/0") != draws(2, "maker/0")
