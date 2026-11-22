"""Tests for the deterministic simulation harness."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from dme.engine import Engine
from dme.events import Cancelled, CancelReason
from dme.sim.__main__ import main
from dme.sim.run import SimConfig, SimulationFailure, run

STEPS = 3_000


def test_runs_exactly_the_requested_number_of_steps() -> None:
    assert run(seed=0, steps=STEPS).steps == STEPS


def test_same_seed_same_digest() -> None:
    assert run(seed=42, steps=STEPS).digest == run(seed=42, steps=STEPS).digest


def test_different_seeds_diverge() -> None:
    assert run(seed=1, steps=STEPS).digest != run(seed=2, steps=STEPS).digest


def test_reference_checking_does_not_change_the_run() -> None:
    """The reference engine only observes; it must not perturb what happens."""
    fast = run(seed=5, steps=STEPS)
    checked = run(seed=5, steps=STEPS, config=SimConfig(reference=True))
    assert fast.digest == checked.digest


def test_digest_is_identical_across_processes_and_hash_seeds() -> None:
    """Catches anything that depends on Python's per-process salted string hashing,
    such as iterating over a set of strings: same seed, same digest, any process."""

    def digest_in_subprocess(hash_seed: str) -> str:
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        out = subprocess.run(
            [sys.executable, "-m", "dme.sim", "--seed", "9", "--steps", str(STEPS)],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout
        return out.split("digest=")[1].split()[0]

    assert digest_in_subprocess("0") == digest_in_subprocess("1") == digest_in_subprocess("2")


def test_workload_reaches_every_outcome() -> None:
    """If a change to the agents stops exercising some path, this fails loudly
    instead of letting the simulator silently test less."""
    counts = run(seed=0, steps=20_000).event_counts
    expected = {
        "Accepted",
        "Rested",
        "Trade",
        "Cancelled:REQUESTED",
        "Cancelled:SELF_TRADE",
        "Expired:UNFILLED",
        "Expired:SELF_TRADE",
        "Rejected:INVALID_PRICE",
        "Rejected:NON_POSITIVE_QTY",
        "CancelRejected:UNKNOWN_ORDER",
    }
    assert expected <= counts.keys(), f"missing: {expected - counts.keys()}"


@pytest.fixture
def owner_index_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plant a real bug: the engine forgets to drop owners of self-trade cancels."""
    original = Engine._track

    def leaky_track(self: Engine, symbol: object, book: object, events: list[object]) -> None:
        kept = [
            e
            for e in events
            if not (isinstance(e, Cancelled) and e.reason is CancelReason.SELF_TRADE)
        ]
        original(self, symbol, book, kept)  # type: ignore[arg-type]

    monkeypatch.setattr(Engine, "_track", leaky_track)


@pytest.mark.usefixtures("owner_index_leak")
def test_planted_bug_is_caught_and_reproducible() -> None:
    with pytest.raises(SimulationFailure) as first:
        run(seed=0, steps=50_000)
    failure = first.value
    assert "owner index" in str(failure.cause)
    assert f"--seed 0 --steps {failure.step + 1}" in failure.repro_command

    # Replaying exactly the reported number of steps fails at exactly the same step.
    with pytest.raises(SimulationFailure) as again:
        run(seed=0, steps=failure.step + 1)
    assert again.value.step == failure.step
    assert str(again.value.cause) == str(failure.cause)


def test_cli_reports_passing_seeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seeds", "0:2", "--steps", "500"]) == 0
    out = capsys.readouterr().out
    assert "seed=0 " in out and "seed=1 " in out and "2 seeds passed" in out


@pytest.mark.usefixtures("owner_index_leak")
def test_cli_reports_failure_with_repro_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seed", "0", "--steps", "50000"]) == 1
    err = capsys.readouterr().err
    assert "reproduce with: python -m dme.sim --seed 0 --steps" in err
    assert "last 20 commands" in err
