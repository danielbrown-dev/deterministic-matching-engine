"""Tests for the deterministic simulation harness, with and without faults."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator

import pytest

from dme import gateway, persistence
from dme.engine import Engine
from dme.events import Cancelled, CancelReason
from dme.sim import node as sim_node
from dme.sim.__main__ import main
from dme.sim.run import SimConfig, SimulationFailure, run
from dme.sim.session import ClientSession

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


# Planted bugs. Each fixture breaks one guarantee; the simulator must catch it, and the
# reported repro command must reproduce the identical failure.


PlantBug = Callable[[pytest.MonkeyPatch], None]


def owner_index_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine forgets to drop owners of orders removed by self-trade prevention."""
    original = Engine._track

    def leaky(self: Engine, symbol: object, book: object, events: list[object]) -> None:
        kept = [
            e
            for e in events
            if not (isinstance(e, Cancelled) and e.reason is CancelReason.SELF_TRADE)
        ]
        original(self, symbol, book, kept)  # type: ignore[arg-type]

    monkeypatch.setattr(Engine, "_track", leaky)


def no_deduplication(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway forgets requests it has seen, so a retry is entered again."""
    original = gateway.Exchange.handle

    def forgetful(self: gateway.Exchange, request: gateway.Request) -> list[gateway.Report]:
        if not isinstance(request, gateway.ResendRequest):
            self._requests.pop((request.trader, request.client_order_id), None)
        return original(self, request)

    monkeypatch.setattr(gateway.Exchange, "handle", forgetful)


def recovery_skips_last_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery replays the log but drops its final entry."""
    original = persistence.recover

    def lossy(storage: persistence.Storage, symbols: object) -> gateway.Exchange:
        trimmed = persistence.Storage(storage.log[:-1], storage.snapshot)
        if storage.snapshot and storage.snapshot.log_position > len(trimmed.log):
            trimmed.snapshot = None
        return original(trimmed, symbols)  # type: ignore[arg-type]

    monkeypatch.setattr(sim_node, "recover", lossy)


def client_skips_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client processes a duplicated report a second time."""
    original = ClientSession.receive

    def gullible(self: ClientSession, report: gateway.Report) -> None:
        if report.seq < self.next_expected:
            self._deliver(report)
            return
        original(self, report)

    monkeypatch.setattr(ClientSession, "receive", gullible)


PLANTED_BUGS: list[tuple[PlantBug, str, str]] = [
    (owner_index_leak, "none", "owner index"),
    (no_deduplication, "net", "accepted twice"),
    (recovery_skips_last_entry, "crash", "recovered state differs"),
    (client_skips_dedup, "net", "expected report #"),
]


@pytest.fixture(params=PLANTED_BUGS, ids=lambda p: p[0].__name__)
def planted(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, str]]:
    plant, faults, message = request.param
    plant(monkeypatch)
    yield faults, message


def test_planted_bug_is_caught_and_reproducible(planted: tuple[str, str]) -> None:
    faults, message = planted
    with pytest.raises(SimulationFailure) as first:
        run(seed=0, steps=50_000, config=config(faults))
    failure = first.value
    assert message in str(failure.cause)
    assert f"--seed 0 --steps {failure.repro_steps}" in failure.repro_command

    with pytest.raises(SimulationFailure) as again:
        run(seed=0, steps=failure.repro_steps, config=config(faults))
    assert again.value.step == failure.step
    assert str(again.value.cause) == str(failure.cause)


def test_cli_reports_passing_seeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seeds", "0:2", "--steps", "500", "--faults", "all"]) == 0
    out = capsys.readouterr().out
    assert "seed=0 " in out and "seed=1 " in out and "2 seeds passed" in out


def test_cli_reports_failure_with_repro_command(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    no_deduplication(monkeypatch)
    assert main(["--seed", "0", "--steps", "50000", "--faults", "net"]) == 1
    err = capsys.readouterr().err
    assert "reproduce with: python -m dme.sim --seed 0 --steps" in err
    assert "--faults net" in err
    assert "requests logged" in err
