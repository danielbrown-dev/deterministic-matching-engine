"""Example-based tests for what the engine adds on top of the books."""

from __future__ import annotations

import pytest

from dme.core import OrderId, OrderSpec, Side, Symbol, TraderId
from dme.engine import Engine, Sequenced
from dme.events import (
    Accepted,
    Cancelled,
    CancelReason,
    CancelRejected,
    CancelRejectReason,
    Rejected,
    RejectReason,
    Rested,
    Trade,
)

AAPL, MSFT = Symbol("AAPL"), Symbol("MSFT")
ALICE, BOB = TraderId(1), TraderId(2)
BUY, SELL = Side.BUY, Side.SELL


@pytest.fixture
def engine() -> Engine:
    return Engine([AAPL, MSFT])


def order_id_of(published: list[Sequenced]) -> OrderId:
    first = published[0].event
    assert isinstance(first, Accepted | Rejected)
    return first.order_id


def test_order_ids_are_unique_across_symbols(engine: Engine) -> None:
    a = order_id_of(engine.submit(AAPL, OrderSpec.limit(ALICE, BUY, 100, 1)))
    b = order_id_of(engine.submit(MSFT, OrderSpec.limit(ALICE, BUY, 100, 1)))
    c = order_id_of(engine.submit(AAPL, OrderSpec.limit(ALICE, BUY, 100, 1)))
    assert (a, b, c) == (OrderId(1), OrderId(2), OrderId(3))


def test_sequence_numbers_are_global_and_contiguous(engine: Engine) -> None:
    out = engine.submit(AAPL, OrderSpec.limit(ALICE, SELL, 100, 1))
    out += engine.submit(MSFT, OrderSpec.limit(ALICE, SELL, 100, 1))
    out += engine.submit(AAPL, OrderSpec.limit(BOB, BUY, 100, 1))
    assert [s.seq for s in out] == list(range(1, len(out) + 1))
    assert [s.symbol for s in out] == [AAPL, AAPL, MSFT, MSFT, AAPL, AAPL]


def test_symbols_never_match_each_other(engine: Engine) -> None:
    engine.submit(AAPL, OrderSpec.limit(ALICE, SELL, 100, 1))
    out = engine.submit(MSFT, OrderSpec.limit(BOB, BUY, 100, 1))
    assert [type(s.event) for s in out] == [Accepted, Rested]


def test_self_trade_prevention_is_per_symbol(engine: Engine) -> None:
    engine.submit(AAPL, OrderSpec.limit(ALICE, SELL, 100, 1))
    engine.submit(MSFT, OrderSpec.limit(BOB, SELL, 100, 1))
    out = engine.submit(MSFT, OrderSpec.limit(ALICE, BUY, 100, 1))
    assert isinstance(out[1].event, Trade)


def test_unknown_symbol_is_rejected(engine: Engine) -> None:
    out = engine.submit(Symbol("TSLA"), OrderSpec.limit(ALICE, BUY, 100, 1))
    assert out == [Sequenced(1, Symbol("TSLA"), Rejected(OrderId(1), RejectReason.UNKNOWN_SYMBOL))]


def test_owner_can_cancel(engine: Engine) -> None:
    order_id = order_id_of(engine.submit(AAPL, OrderSpec.limit(ALICE, BUY, 100, 3)))
    out = engine.cancel(ALICE, order_id)
    assert [s.event for s in out] == [Cancelled(order_id, 3, CancelReason.REQUESTED)]
    assert out[0].symbol == AAPL
    assert engine.resting_owner(order_id) is None


def test_other_trader_cannot_cancel_and_learns_nothing(engine: Engine) -> None:
    order_id = order_id_of(engine.submit(AAPL, OrderSpec.limit(ALICE, BUY, 100, 3)))
    by_bob = engine.cancel(BOB, order_id)
    never_existed = engine.cancel(BOB, OrderId(999))
    rejection = CancelRejected(order_id, CancelRejectReason.UNKNOWN_ORDER)
    assert by_bob == [Sequenced(by_bob[0].seq, None, rejection)]
    # Indistinguishable from an ID that never existed:
    assert never_existed[0].symbol is None
    assert type(never_existed[0].event) is CancelRejected
    assert engine.resting_owner(order_id) == (AAPL, ALICE)


def test_filled_order_can_no_longer_be_cancelled(engine: Engine) -> None:
    order_id = order_id_of(engine.submit(AAPL, OrderSpec.limit(ALICE, SELL, 100, 2)))
    engine.submit(AAPL, OrderSpec.limit(BOB, BUY, 100, 2))
    assert engine.resting_owner(order_id) is None
    assert type(engine.cancel(ALICE, order_id)[0].event) is CancelRejected


def test_partially_filled_order_keeps_its_owner(engine: Engine) -> None:
    order_id = order_id_of(engine.submit(AAPL, OrderSpec.limit(ALICE, SELL, 100, 5)))
    engine.submit(AAPL, OrderSpec.limit(BOB, BUY, 100, 2))
    assert engine.resting_owner(order_id) == (AAPL, ALICE)
    assert engine.cancel(ALICE, order_id)[0].event == Cancelled(order_id, 3, CancelReason.REQUESTED)


def test_self_trade_cancel_clears_owner(engine: Engine) -> None:
    order_id = order_id_of(engine.submit(AAPL, OrderSpec.limit(ALICE, SELL, 100, 5)))
    engine.submit(AAPL, OrderSpec.limit(ALICE, BUY, 100, 1))  # default: cancel resting
    assert engine.resting_owner(order_id) is None
