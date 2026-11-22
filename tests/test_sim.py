"""Tests for the deterministic simulation harness, with and without faults."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from dme.sim.__main__ import main
from dme.sim.bugs import CATALOGUE, PlantedBug
from dme.sim.run import SimConfig, SimulationFailure, run

STEPS = 3_000
FAULT_MODES = ["none", "net", "crash", "all"]


def config(faults: str = "none", **kwargs: object) -> SimConfig:
    return SimConfig(faults=faults, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("faults", FAULT_MODES)
def test_runs_at_least_the_requested_steps(faults: str) -> None:
    # Steps count logged requests; the end-of-run drain may log a few more.
    assert run(seed=0, steps=STEPS, config=config(faults)).steps >= STEPS


@pytest.mark.parametrize("faults", FAULT_MODES)
def test_same_seed_same_digest(faults: str) -> None:
    first = run(seed=42, steps=STEPS, config=config(faults))
    assert first.digest == run(seed=42, steps=STEPS, config=config(faults)).digest


def test_different_seeds_diverge() -> None:
    assert run(seed=1, steps=STEPS).digest != run(seed=2, steps=STEPS).digest


def test_reference_checking_does_not_change_the_run() -> None:
    """The reference engine only observes; it must not perturb what happens."""
    fast = run(seed=5, steps=STEPS, config=config("all"))
    checked = run(seed=5, steps=STEPS, config=config("all", reference=True))
    assert fast.digest == checked.digest


def test_digest_is_identical_across_processes_and_hash_seeds() -> None:
    """Catches anything that depends on Python's per-process salted string hashing,
    such as iterating over a set of strings: same seed, same digest, any process."""

    def digest_in_subprocess(hash_seed: str) -> str:
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        command = [sys.executable, "-m", "dme.sim", "--seed", "9", "--steps", str(STEPS)]
        out = subprocess.run(
            [*command, "--faults", "all"], capture_output=True, text=True, check=True, env=env
        ).stdout
        return out.split("digest=")[1].split()[0]

    assert digest_in_subprocess("0") == digest_in_subprocess("1") == digest_in_subprocess("2")


def test_faults_actually_happen() -> None:
    counts = run(seed=0, steps=10_000, config=config("all")).counts
    assert counts["net:dropped"] > 0
    assert counts["net:duplicated"] > 0
    assert counts["node:crashes"] > 0
    assert counts["node:mid_request_crashes"] > 0
    assert counts["node:recoveries"] == counts["node:crashes"]


def test_workload_reaches_every_outcome() -> None:
    """If a change to the agents stops exercising some path, this fails loudly
    instead of letting the simulator silently test less."""
    counts = run(seed=0, steps=20_000).counts
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
        "GatewayReject:UNKNOWN_ORDER",
        "GatewayReject:WRONG_TRADER",
        "request:ResendRequest",
    }
    assert expected <= counts.keys(), f"missing: {expected - counts.keys()}"


# Planted bugs: the simulator must catch every bug in the catalogue, and the reported
# repro command must reproduce the identical failure.


@pytest.mark.parametrize("bug", CATALOGUE, ids=lambda bug: bug.name)
def test_planted_bug_is_caught_and_reproducible(bug: PlantedBug) -> None:
    bugged = config(bug.faults, planted_bug=bug.name)
    with pytest.raises(SimulationFailure) as first:
        run(seed=0, steps=50_000, config=bugged)
    failure = first.value
    assert bug.expect in str(failure.cause)
    assert f"--steps {failure.repro_steps}" in failure.repro_command
    assert f"--bug {bug.name}" in failure.repro_command

    with pytest.raises(SimulationFailure) as again:
        run(seed=0, steps=failure.repro_steps, config=bugged)
    assert again.value.step == failure.step
    assert str(again.value.cause) == str(failure.cause)


def test_planted_bugs_are_removed_afterwards() -> None:
    for bug in CATALOGUE:
        with pytest.raises(SimulationFailure):
            run(seed=0, steps=50_000, config=config(bug.faults, planted_bug=bug.name))
    run(seed=0, steps=STEPS, config=config("all"))  # passes again


def test_cli_reports_passing_seeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seeds", "0:2", "--steps", "500", "--faults", "all"]) == 0
    out = capsys.readouterr().out
    assert "seed=0 " in out and "seed=1 " in out and "2 seeds passed" in out


def test_cli_reports_failure_with_repro_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seed", "0", "--steps", "50000", "--bug", "no-dedup"]) == 1
    err = capsys.readouterr().err
    assert "reproduce with: python -m dme.sim --seed 0 --steps" in err
    assert "--faults net --bug no-dedup" in err
    assert "requests logged" in err


def test_cli_bug_demo_catches_everything(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--bugs"]) == 0
    out = capsys.readouterr().out
    assert "MISSED" not in out
    for bug in CATALOGUE:
        assert bug.name in out
