"""Write-ahead log, snapshots and crash recovery.

The exchange is deterministic, so its state is a pure function of the requests it
has handled. That makes durability simple:

  1. Before handling a request, append it to the log (write-ahead). Only then apply
     it and send the reports.
  2. Every so often, store an immutable snapshot of the exchange state together with
     the log position it covers.
  3. To recover, rebuild from the latest snapshot and replay the log after it.

A crash may hit between steps 1 and the reports going out. Recovery then applies the
logged request, but its reports were never sent. That is safe: the client's retry is
recognised as a duplicate, and the reports are delivered through the normal
resend mechanism. A crash *before* the append loses the request entirely, which is
also safe: the exchange never acted on it, and the client retries.

``Storage`` stands in for a disk. In the simulator it is the one object that survives
a crash.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from dme.core import Symbol
from dme.engine import Engine
from dme.gateway import Exchange, ExchangeState, Request


@dataclass(frozen=True, slots=True)
class Snapshot:
    log_position: int
    """Number of log entries already reflected in ``state``."""
    state: ExchangeState


@dataclass
class Storage:
    log: list[Request] = field(default_factory=list)
    snapshot: Snapshot | None = None

    def append(self, request: Request) -> None:
        self.log.append(request)

    def save_snapshot(self, state: ExchangeState) -> None:
        self.snapshot = Snapshot(len(self.log), state)


def recover(storage: Storage, symbols: Iterable[Symbol]) -> Exchange:
    """Rebuild the exchange from storage. Reports produced while replaying are
    discarded: they were either delivered before the crash or will be resent."""
    if storage.snapshot is None:
        exchange = Exchange(Engine(symbols))
        start = 0
    else:
        exchange = Exchange.from_state(storage.snapshot.state)
        start = storage.snapshot.log_position
    for request in storage.log[start:]:
        exchange.handle(request)
    return exchange
