"""Shared differential-testing machinery.

``BookPair`` drives the real order book and the reference model in lockstep;
``EnginePair`` does the same for an engine built on each. After every command both
check that the two agree exactly and that every invariant and audit rule holds.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from dme.audit import Audit
from dme.book import OrderBook
from dme.core import (
    OrderId,
    OrderSpec,
    OrderType,
    RestingOrder,
    SelfTradePrevention,
    Side,
    Symbol,
    TraderId,
)
from dme.engine import Engine, Sequenced
from dme.events import CancelRejected, Event, Trade
from dme.reference import ReferenceBook


@dataclass(frozen=True)
class Submit:
    order_id: OrderId
    spec: OrderSpec


@dataclass(frozen=True)
class Cancel:
    order_id: OrderId


BookCommand = Submit | Cancel


class BookPair:
    """The real book and the reference model, driven in lockstep."""

    def __init__(self) -> None:
        self.book = OrderBook()
        self.reference = ReferenceBook()
        self.audit = Audit()

    def apply(self, command: BookCommand) -> list[Event]:
        if isinstance(command, Submit):
            actual = self.book.submit(command.order_id, command.spec)
            expected = self.reference.submit(command.order_id, command.spec)
        else:
            actual = self.book.cancel(command.order_id)
            expected = self.reference.cancel(command.order_id)

        assert actual == expected, f"{command}: book emitted {actual}, reference {expected}"
        if isinstance(command, Submit):
            self.audit.submitted(actual)
        else:
            self.audit.cancel_requested(command.order_id, None, actual)
        self.book.check_invariants()
        snapshot = self.book.snapshot()
        assert snapshot == self.reference.snapshot(), f"{command}: snapshots diverged"
        on_book = {order.order_id: order for order in snapshot.bids + snapshot.asks}
        assert on_book == self.audit.resting(), f"{command}: book disagrees with the audit"
        return actual


class EnginePair:
    """An engine on real books and an engine on reference books, in lockstep."""

    def __init__(self, symbols: list[Symbol]) -> None:
        self.symbols = symbols
        self.books: list[OrderBook] = []

        def make_book() -> OrderBook:
            book = OrderBook()
            self.books.append(book)
            return book

        self.engine = Engine(symbols, make_book)
        self.reference = Engine(symbols, ReferenceBook)
        self.audit = Audit()
        self.last_seq = 0

    def submit(self, symbol: Symbol, spec: OrderSpec) -> list[Sequenced]:
        actual = self.engine.submit(symbol, spec)
        expected = self.reference.submit(symbol, spec)
        self._compare(f"submit {symbol} {spec}", actual, expected)
        self.audit.submitted([s.event for s in actual])
        self._check(f"submit {symbol} {spec}", actual)
        return actual

    def cancel(self, trader: TraderId, order_id: OrderId) -> list[Sequenced]:
        actual = self.engine.cancel(trader, order_id)
        expected = self.reference.cancel(trader, order_id)
        self._compare(f"cancel {order_id} by {trader}", actual, expected)
        self.audit.cancel_requested(order_id, trader, [s.event for s in actual])
        self._check(f"cancel {order_id} by {trader}", actual)
        return actual

    def _compare(self, what: str, actual: list[Sequenced], expected: list[Sequenced]) -> None:
        assert actual == expected, f"{what}: engine emitted {actual}, reference {expected}"
        seqs = [s.seq for s in actual]
        assert seqs == list(range(self.last_seq + 1, self.last_seq + 1 + len(seqs))), (
            f"{what}: sequence numbers not contiguous: {seqs} after {self.last_seq}"
        )
        self.last_seq += len(seqs)
        assert len({s.symbol for s in actual}) <= 1, f"{what}: events span several symbols"

    def _check(self, what: str, published: list[Sequenced]) -> None:
        resting = self.audit.resting()
        for s in published:
            if isinstance(s.event, CancelRejected):
                # Must not reveal which symbol an order (or someone else's order) is on.
                assert s.symbol is None, f"{what}: rejected cancel names a symbol"
            # Every order this command touched and that is no longer resting must be
            # gone from the engine's owner index, or it leaks.
            for order_id in _order_ids(s.event):
                if order_id not in resting:
                    owner = self.engine.resting_owner(order_id)
                    assert owner is None, f"{what}: stale owner for order {order_id}"

        on_books: dict[OrderId, RestingOrder] = {}
        for book in self.books:
            book.check_invariants()
        for symbol in self.symbols:
            snapshot = self.engine.snapshot(symbol)
            assert snapshot == self.reference.snapshot(symbol), f"{what}: {symbol} diverged"
            for order in snapshot.bids + snapshot.asks:
                on_books[order.order_id] = order
                assert self.engine.resting_owner(order.order_id) == (symbol, order.trader)
        assert on_books == resting, f"{what}: books disagree with the audit"


def _order_ids(event: Event) -> tuple[OrderId, ...]:
    if isinstance(event, Trade):
        return (event.maker_id, event.taker_id)
    return (event.order_id,)


def random_spec(rng: random.Random, traders: int = 3) -> OrderSpec:
    """A random order spec: few traders so self-trades are common, prices in a
    narrow band so orders cross often, and an occasional invalid field."""
    trader = TraderId(rng.randrange(traders))
    side = rng.choice([Side.BUY, Side.SELL])
    stp = rng.choice(list(SelfTradePrevention))
    roll = rng.random()
    order_type = (
        OrderType.LIMIT if roll < 0.7 else OrderType.IOC if roll < 0.85 else OrderType.MARKET
    )
    price: int | None = None if order_type is OrderType.MARKET else rng.randint(95, 105)
    if rng.random() < 0.02:
        price = 100 if order_type is OrderType.MARKET else rng.choice([0, -1, None])
    qty = rng.randint(1, 10) if rng.random() < 0.98 else rng.choice([0, -3])
    return OrderSpec(trader, side, order_type, price, qty, stp)
