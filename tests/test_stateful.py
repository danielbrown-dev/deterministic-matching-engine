"""Hypothesis stateful tests: random command sequences against the reference model.

Hypothesis explores sequences the seeded tests would rarely hit, and when it finds
a failure it shrinks it to a minimal sequence of commands.
"""

from __future__ import annotations

from harness import BookPair, Cancel, EnginePair, Submit
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

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

# Few traders so self-trades are common; a narrow price band so orders cross often;
# the occasional invalid field.
traders = st.integers(min_value=0, max_value=2).map(TraderId)
sides = st.sampled_from(Side)
stps = st.sampled_from(SelfTradePrevention)
qtys = st.integers(min_value=1, max_value=10) | st.sampled_from([0, -1])
priced = st.builds(
    OrderSpec,
    traders,
    sides,
    st.sampled_from([OrderType.LIMIT, OrderType.IOC]),
    st.integers(min_value=95, max_value=105) | st.sampled_from([0, -1, None]),
    qtys,
    stps,
)
market = st.builds(
    OrderSpec, traders, sides, st.just(OrderType.MARKET), st.none() | st.just(100), qtys, stps
)
specs = priced | market

SYMBOLS = [Symbol("AAPL"), Symbol("MSFT")]


class BookVersusReference(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.pair = BookPair()
        self.next_id = 0

    @rule(spec=specs)
    def submit_new(self, spec: OrderSpec) -> None:
        self.pair.apply(Submit(OrderId(self.next_id), spec))
        self.next_id += 1

    @rule(data=st.data(), spec=specs)
    def submit_reused_id(self, data: st.DataObject, spec: OrderSpec) -> None:
        order_id = data.draw(st.integers(min_value=0, max_value=self.next_id))
        self.pair.apply(Submit(OrderId(order_id), spec))

    @rule(data=st.data())
    def cancel(self, data: st.DataObject) -> None:
        order_id = data.draw(st.integers(min_value=0, max_value=self.next_id + 2))
        self.pair.apply(Cancel(OrderId(order_id)))

    @invariant()
    def best_prices_agree_with_snapshot(self) -> None:
        book = self.pair.book
        snapshot = book.snapshot()
        assert book.best_bid() == (snapshot.bids[0].price if snapshot.bids else None)
        assert book.best_ask() == (snapshot.asks[0].price if snapshot.asks else None)


class EngineVersusReference(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.pair = EnginePair(SYMBOLS)
        self.issued = 0

    @rule(symbol=st.sampled_from(SYMBOLS), spec=specs)
    def submit(self, symbol: Symbol, spec: OrderSpec) -> None:
        self.pair.submit(symbol, spec)
        self.issued += 1

    @rule(data=st.data(), trader=traders)
    def cancel(self, data: st.DataObject, trader: TraderId) -> None:
        order_id = data.draw(st.integers(min_value=1, max_value=self.issued + 2))
        self.pair.cancel(trader, OrderId(order_id))

    @precondition(lambda self: bool(self.pair.audit.resting()))
    @rule(data=st.data(), by_owner=st.booleans(), other=traders)
    def cancel_resting(self, data: st.DataObject, by_owner: bool, other: TraderId) -> None:
        order = data.draw(st.sampled_from(sorted(self.pair.audit.resting().values(), key=_id)))
        self.pair.cancel(order.trader if by_owner else other, order.order_id)


def _id(order: RestingOrder) -> int:
    return order.order_id


_settings = settings(max_examples=300, stateful_step_count=60, deadline=None)
BookVersusReference.TestCase.settings = _settings
EngineVersusReference.TestCase.settings = _settings
TestBookVersusReference = BookVersusReference.TestCase
TestEngineVersusReference = EngineVersusReference.TestCase
