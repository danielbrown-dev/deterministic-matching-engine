"""Hypothesis stateful test: random command sequences, book vs reference model.

Hypothesis explores sequences the seeded test would rarely hit, and when it finds
a failure it shrinks it to a minimal sequence of commands.
"""

from __future__ import annotations

from harness import Cancel, Pair, Submit
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from dme.core import OrderId, Side

sides = st.sampled_from(Side)
# A narrow band so orders cross often, plus the occasional invalid value.
prices = st.integers(min_value=95, max_value=105) | st.sampled_from([0, -1])
qtys = st.integers(min_value=1, max_value=10) | st.sampled_from([0, -1])


class BookVersusReference(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.pair = Pair()
        self.next_id = 0

    @rule(side=sides, price=prices, qty=qtys)
    def submit_new(self, side: Side, price: int, qty: int) -> None:
        self.pair.apply(Submit(OrderId(self.next_id), side, price, qty))
        self.next_id += 1

    @rule(data=st.data(), side=sides, price=prices, qty=qtys)
    def submit_reused_id(self, data: st.DataObject, side: Side, price: int, qty: int) -> None:
        order_id = data.draw(st.integers(min_value=0, max_value=self.next_id))
        self.pair.apply(Submit(OrderId(order_id), side, price, qty))

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


BookVersusReference.TestCase.settings = settings(max_examples=300, stateful_step_count=60)
TestBookVersusReference = BookVersusReference.TestCase
