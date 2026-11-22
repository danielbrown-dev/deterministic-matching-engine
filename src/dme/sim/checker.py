"""Runs commands through an engine and checks every invariant after each one.

This is the oracle the simulator (and the engine-level tests) rely on. After every
command it checks:

  * the event stream obeys every ``Audit`` rule;
  * every book's internal structure is consistent (``check_invariants``);
  * sequence numbers are contiguous and one command's events name one symbol;
  * rejected cancels name no symbol, so they cannot leak where an order lives;
  * the engine's owner index holds exactly the resting orders, no more;
  * the books hold exactly what the audit says should be resting;
  * optionally, an engine on ``ReferenceBook``s produced identical events and books.

The reference comparison is optional because the reference model scans every resting
order on every command: thorough, but too slow for long simulations.

Every failure raises ``InvariantViolation``, never ``assert``, so checks still run
under ``python -O``.
"""

from __future__ import annotations

from collections.abc import Iterable

from dme.audit import Audit
from dme.book import OrderBook
from dme.core import InvariantViolation, OrderId, RestingOrder, Symbol
from dme.engine import Command, Engine, Sequenced, SubmitOrder
from dme.events import CancelRejected, Event, Trade
from dme.reference import ReferenceBook


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvariantViolation(message)


class EngineChecker:
    def __init__(self, symbols: Iterable[Symbol], *, reference: bool) -> None:
        self.symbols = tuple(symbols)
        self._books: list[OrderBook] = []

        def make_book() -> OrderBook:
            book = OrderBook()
            self._books.append(book)
            return book

        self.engine = Engine(self.symbols, make_book)
        self.reference = Engine(self.symbols, ReferenceBook) if reference else None
        self.audit = Audit()
        self._last_seq = 0

    def apply(self, command: Command) -> list[Sequenced]:
        published = self.engine.apply(command)
        if self.reference is not None:
            expected = self.reference.apply(command)
            _require(published == expected, f"engine emitted {published}, reference {expected}")

        seqs = [s.seq for s in published]
        expected_seqs = list(range(self._last_seq + 1, self._last_seq + 1 + len(seqs)))
        _require(seqs == expected_seqs, f"sequence numbers {seqs} after {self._last_seq}")
        self._last_seq += len(seqs)
        _require(len({s.symbol for s in published}) <= 1, "one command's events span symbols")

        events = [s.event for s in published]
        if isinstance(command, SubmitOrder):
            self.audit.submitted(events)
        else:
            self.audit.cancel_requested(command.order_id, command.trader, events)

        self._check_state(published)
        return published

    def _check_state(self, published: list[Sequenced]) -> None:
        resting = self.audit.resting()
        for s in published:
            if isinstance(s.event, CancelRejected):
                _require(s.symbol is None, f"rejected cancel names a symbol: {s}")
            for order_id in _order_ids(s.event):
                if order_id not in resting:
                    owner = self.engine.resting_owner(order_id)
                    _require(owner is None, f"owner index keeps non-resting order {order_id}")

        for book in self._books:
            book.check_invariants()

        on_books: dict[OrderId, RestingOrder] = {}
        for symbol in self.symbols:
            snapshot = self.engine.snapshot(symbol)
            if self.reference is not None:
                _require(snapshot == self.reference.snapshot(symbol), f"{symbol} book diverged")
            for order in snapshot.bids + snapshot.asks:
                on_books[order.order_id] = order
                owner = self.engine.resting_owner(order.order_id)
                _require(owner == (symbol, order.trader), f"wrong owner for {order}: {owner}")
        _require(on_books == resting, "books disagree with the audit")


def _order_ids(event: Event) -> tuple[OrderId, ...]:
    if isinstance(event, Trade):
        return (event.maker_id, event.taker_id)
    return (event.order_id,)
