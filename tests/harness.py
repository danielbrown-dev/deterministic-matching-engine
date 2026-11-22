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
    SelfTradePrevention,
    Side,
    Symbol,
    TraderId,
)
from dme.engine import CancelOrder, Sequenced, SubmitOrder
from dme.events import Event
from dme.reference import ReferenceBook
from dme.sim.checker import EngineChecker


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
    """An engine on real books, checked after every command against an engine on
    reference books and every other invariant (see ``dme.sim.checker``)."""

    def __init__(self, symbols: list[Symbol]) -> None:
        self.checker = EngineChecker(symbols, reference=True)
        self.engine = self.checker.engine
        self.audit = self.checker.audit

    def submit(self, symbol: Symbol, spec: OrderSpec) -> list[Sequenced]:
        return self.checker.apply(SubmitOrder(symbol, spec))

    def cancel(self, trader: TraderId, order_id: OrderId) -> list[Sequenced]:
        return self.checker.apply(CancelOrder(trader, order_id))


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
