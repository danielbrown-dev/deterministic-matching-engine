"""A catalogue of realistic bugs that can be planted on purpose.

Each entry breaks one guarantee in a way that a plausible code change could. They
exist to show that the checks work: a test suite that has never caught a bug has not
shown that it can. Run one with ``python -m dme.sim --bug NAME``, or all of them with
``python -m dme.sim --bugs``.

Bugs are planted by patching the running code and are always removed afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from dme import gateway, persistence
from dme.book import _Level, _Order
from dme.engine import Engine
from dme.events import Cancelled, CancelReason
from dme.sim import node as sim_node
from dme.sim import run as sim_run
from dme.sim.session import ClientSession


@dataclass(frozen=True)
class PlantedBug:
    name: str
    description: str
    faults: str
    """The fault mode that exposes it."""
    expect: str
    """Text the resulting failure must contain."""
    patches: Callable[[], list[tuple[object, str, Any]]]
    """Returns (owner, attribute, replacement) triples to install."""


@contextmanager
def planted(bug: PlantedBug) -> Iterator[None]:
    patches = bug.patches()
    originals = [(owner, name, getattr(owner, name)) for owner, name, _ in patches]
    try:
        for owner, name, replacement in patches:
            setattr(owner, name, replacement)
        yield
    finally:
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)


def _stale_level_total() -> list[tuple[object, str, Any]]:
    original = _Level.unlink

    def unlink(self: _Level, order: _Order) -> None:
        qty = order.qty
        original(self, order)
        self.total_qty += qty  # forgets to subtract the departing quantity

    return [(_Level, "unlink", unlink)]


def _owner_index_leak() -> list[tuple[object, str, Any]]:
    original = Engine._track

    def track(self: Engine, symbol: Any, book: Any, events: list[Any]) -> None:
        kept = [
            e
            for e in events
            if not (isinstance(e, Cancelled) and e.reason is CancelReason.SELF_TRADE)
        ]
        original(self, symbol, book, kept)

    return [(Engine, "_track", track)]


def _no_deduplication() -> list[tuple[object, str, Any]]:
    original = gateway.Exchange.handle

    def handle(self: gateway.Exchange, request: gateway.Request) -> list[gateway.Report]:
        if not isinstance(request, gateway.ResendRequest):
            self._requests.pop((request.trader, request.client_order_id), None)
        return original(self, request)

    return [(gateway.Exchange, "handle", handle)]


def _recovery_skips_last_entry() -> list[tuple[object, str, Any]]:
    original = persistence.recover

    def recover(storage: persistence.Storage, symbols: Any) -> gateway.Exchange:
        trimmed = persistence.Storage(storage.log[:-1], storage.snapshot)
        if storage.snapshot and storage.snapshot.log_position > len(trimmed.log):
            trimmed.snapshot = None
        return original(trimmed, symbols)

    return [(sim_node, "recover", recover)]


def _client_skips_dedup() -> list[tuple[object, str, Any]]:
    original = ClientSession.receive

    def receive(self: ClientSession, report: gateway.Report) -> None:
        if report.seq < self.next_expected:
            self._deliver(report)  # processes a duplicate again
            return
        original(self, report)

    return [(ClientSession, "receive", receive)]


def _lost_restart() -> list[tuple[object, str, Any]]:
    def on_crash(self: sim_run.Simulation) -> None:
        pass  # a crash that never schedules the process restart

    return [(sim_run.Simulation, "_on_crash", on_crash)]


CATALOGUE: tuple[PlantedBug, ...] = (
    PlantedBug(
        "stale-level-total",
        "Order book: removing an order leaves its quantity in the price level's total",
        "none",
        "cached totals are stale",
        _stale_level_total,
    ),
    PlantedBug(
        "owner-index-leak",
        "Engine: forgets owners of orders removed by self-trade prevention",
        "none",
        "owner index keeps non-resting order",
        _owner_index_leak,
    ),
    PlantedBug(
        "no-dedup",
        "Gateway: forgets requests it has seen, so a retried order is entered twice",
        "net",
        "accepted twice",
        _no_deduplication,
    ),
    PlantedBug(
        "recovery-skips-last",
        "Persistence: recovery replays the log but drops its final entry",
        "crash",
        "recovered state differs",
        _recovery_skips_last_entry,
    ),
    PlantedBug(
        "client-no-dedup",
        "Client session: processes a duplicated report a second time",
        "net",
        "expected report #",
        _client_skips_dedup,
    ),
    PlantedBug(
        "lost-restart",
        "Process supervision: a crash never schedules a restart (a liveness bug)",
        "crash",
        "not being served",
        _lost_restart,
    ),
)

BY_NAME = {bug.name: bug for bug in CATALOGUE}
