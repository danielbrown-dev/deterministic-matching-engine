"""The exchange process: write-ahead log, crashes, recovery, and the shadow oracle.

The node runs two exchanges side by side:

  * the **live** exchange, which crashes and is rebuilt from ``Storage``;
  * the **shadow**, which never crashes and handles every request at the moment it
    is durably logged. Its engine runs under the ``EngineChecker``, so every engine
    command is checked.

Because the exchange is deterministic, the live exchange must send exactly the
reports the shadow produced, and after every recovery its state must equal the
shadow's exactly. Any difference is a durability or recovery bug.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from dme.core import InvariantViolation, Symbol
from dme.engine import Engine
from dme.gateway import Exchange, Report, Request
from dme.persistence import Storage, recover
from dme.sim.checker import EngineChecker


class ExchangeNode:
    def __init__(
        self,
        symbols: tuple[Symbol, ...],
        *,
        reference: bool,
        snapshot_every: int,
        crash_mid_request: float,
        rng: random.Random,
        on_logged: Callable[[Request, list[Report]], None],
        on_crash: Callable[[], None],
    ) -> None:
        self.symbols = symbols
        self.storage = Storage()
        self.checker = EngineChecker(symbols, reference=reference)
        self.shadow = Exchange(self.checker.engine, apply=self.checker.apply)
        self.live: Exchange | None = Exchange(Engine(symbols))
        self.crash_mid_request = crash_mid_request
        self._snapshot_every = snapshot_every
        self._rng = rng
        self._on_logged = on_logged
        self._on_crash = on_crash
        self.crashes = 0
        self.mid_request_crashes = 0
        self.lost_while_down = 0

    @property
    def up(self) -> bool:
        return self.live is not None

    def receive(self, request: Request) -> list[Report]:
        """Handle a request arriving from the network. Returns the reports to send."""
        if self.live is None:
            self.lost_while_down += 1
            return []

        self.storage.append(request)
        expected = self.shadow.handle(request)
        self._on_logged(request, expected)

        if self._rng.random() < self.crash_mid_request:
            self.mid_request_crashes += 1
            self.crash()
            return []

        actual = self.live.handle(request)
        if actual != expected:
            raise InvariantViolation(f"{request}: live sent {actual}, shadow {expected}")
        log = self.storage.log
        snapshot = self.storage.snapshot
        if len(log) - (snapshot.log_position if snapshot else 0) >= self._snapshot_every:
            self.storage.save_snapshot(self.live.state())
        return actual

    def crash(self) -> None:
        """Lose all in-memory state. Whatever caused the crash, ``on_crash`` is told,
        so a restart always gets scheduled."""
        if self.live is not None:
            self.crashes += 1
            self.live = None
            self._on_crash()

    def restart(self) -> None:
        if self.live is not None:
            return
        live = recover(self.storage, self.symbols)
        if live.state() != self.shadow.state():
            raise InvariantViolation("recovered state differs from the shadow exchange")
        self.live = live
