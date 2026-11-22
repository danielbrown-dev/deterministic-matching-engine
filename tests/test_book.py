"""Example-based tests: each pins down one rule of the order book's semantics."""

from __future__ import annotations

import pytest

from dme.book import OrderBook
from dme.core import (
    OrderId,
    OrderSpec,
    OrderType,
    RestingOrder,
    SelfTradePrevention,
    Side,
    TraderId,
)
from dme.events import (
    Accepted,
    Cancelled,
    CancelReason,
    CancelRejected,
    CancelRejectReason,
    Event,
    Expired,
    ExpireReason,
    Rejected,
    RejectReason,
    Rested,
    Trade,
)

BUY, SELL = Side.BUY, Side.SELL
ALICE, BOB, CAROL = TraderId(1), TraderId(2), TraderId(3)
STP = SelfTradePrevention


def oid(n: int) -> OrderId:
    return OrderId(n)


def limit(
    trader: TraderId, side: Side, price: int, qty: int, stp: STP = STP.CANCEL_RESTING
) -> OrderSpec:
    return OrderSpec.limit(trader, side, price, qty, stp)


def trades(events: list[Event]) -> list[Trade]:
    return [e for e in events if isinstance(e, Trade)]


@pytest.fixture
def book() -> OrderBook:
    return OrderBook()


# Limit orders and price-time priority


def test_order_rests_on_empty_book(book: OrderBook) -> None:
    spec = limit(ALICE, BUY, 100, 5)
    assert book.submit(oid(1), spec) == [
        Accepted(oid(1), spec),
        Rested(oid(1), ALICE, BUY, 100, 5),
    ]
    assert book.best_bid() == 100
    assert book.best_ask() is None
    assert book.depth(BUY) == [(100, 5)]


def test_non_crossing_orders_both_rest(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, BUY, 99, 5))
    book.submit(oid(2), limit(BOB, SELL, 101, 5))
    assert (book.best_bid(), book.best_ask()) == (99, 101)
    assert len(book) == 2


def test_equal_prices_cross(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, SELL, 100, 5))
    spec = limit(BOB, BUY, 100, 5)
    assert book.submit(oid(2), spec) == [
        Accepted(oid(2), spec),
        Trade(maker_id=oid(1), taker_id=oid(2), price=100, qty=5),
    ]
    assert len(book) == 0


def test_partial_fill_of_maker_leaves_remainder_at_front(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, SELL, 100, 10))
    book.submit(oid(2), limit(BOB, SELL, 100, 10))
    book.submit(oid(3), limit(CAROL, BUY, 100, 4))
    assert book.snapshot().asks == (
        RestingOrder(oid(1), ALICE, SELL, 100, 6),
        RestingOrder(oid(2), BOB, SELL, 100, 10),
    )
    assert book.depth(SELL) == [(100, 16)]


def test_partial_fill_of_taker_rests_remainder(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, SELL, 100, 3))
    events = book.submit(oid(2), limit(BOB, BUY, 100, 10))
    assert events[1:] == [Trade(oid(1), oid(2), 100, 3), Rested(oid(2), BOB, BUY, 100, 7)]
    assert (book.best_bid(), book.best_ask()) == (100, None)


def test_time_priority_within_a_level(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, BUY, 100, 2))
    book.submit(oid(2), limit(BOB, BUY, 100, 2))
    book.submit(oid(3), limit(ALICE, BUY, 100, 2))
    assert trades(book.submit(oid(4), limit(CAROL, SELL, 100, 5))) == [
        Trade(oid(1), oid(4), 100, 2),
        Trade(oid(2), oid(4), 100, 2),
        Trade(oid(3), oid(4), 100, 1),
    ]


def test_price_priority_beats_time_priority(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, SELL, 102, 1))  # older but worse
    book.submit(oid(2), limit(BOB, SELL, 101, 1))
    assert trades(book.submit(oid(3), limit(CAROL, BUY, 102, 1))) == [Trade(oid(2), oid(3), 101, 1)]
    assert book.best_ask() == 102


def test_sweep_trades_at_each_makers_price(book: OrderBook) -> None:
    for n, price in enumerate([101, 102, 103, 104], start=1):
        book.submit(oid(n), limit(ALICE, SELL, price, 1))
    events = book.submit(oid(5), limit(BOB, BUY, 103, 10))
    assert events[1:] == [
        Trade(oid(1), oid(5), 101, 1),
        Trade(oid(2), oid(5), 102, 1),
        Trade(oid(3), oid(5), 103, 1),
        Rested(oid(5), BOB, BUY, 103, 7),
    ]
    assert (book.best_bid(), book.best_ask()) == (103, 104)


def test_sell_taker_sweeps_bids_from_highest(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, BUY, 98, 1))
    book.submit(oid(2), limit(ALICE, BUY, 100, 1))
    book.submit(oid(3), limit(ALICE, BUY, 99, 1))
    assert trades(book.submit(oid(4), limit(BOB, SELL, 99, 3))) == [
        Trade(oid(2), oid(4), 100, 1),
        Trade(oid(3), oid(4), 99, 1),
    ]
    assert (book.best_bid(), book.best_ask()) == (98, 99)


# IOC and market orders


def test_ioc_fills_what_it_can_and_expires_the_rest(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, SELL, 100, 3))
    book.submit(oid(2), limit(ALICE, SELL, 102, 3))
    events = book.submit(oid(3), OrderSpec.ioc(BOB, BUY, 101, 10))
    assert events[1:] == [
        Trade(oid(1), oid(3), 100, 3),
        Expired(oid(3), 7, ExpireReason.UNFILLED),
    ]
    assert book.best_bid() is None  # never rests
    assert book.best_ask() == 102


def test_ioc_with_nothing_to_match_expires_in_full(book: OrderBook) -> None:
    events = book.submit(oid(1), OrderSpec.ioc(ALICE, BUY, 100, 5))
    assert events[1:] == [Expired(oid(1), 5, ExpireReason.UNFILLED)]
    assert len(book) == 0


def test_market_order_sweeps_any_price(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, BUY, 100, 2))
    book.submit(oid(2), limit(ALICE, BUY, 50, 2))
    events = book.submit(oid(3), OrderSpec.market(BOB, SELL, 3))
    assert events[1:] == [Trade(oid(1), oid(3), 100, 2), Trade(oid(2), oid(3), 50, 1)]
    assert book.depth(BUY) == [(50, 1)]


def test_market_order_on_empty_book_expires(book: OrderBook) -> None:
    events = book.submit(oid(1), OrderSpec.market(ALICE, BUY, 5))
    assert events[1:] == [Expired(oid(1), 5, ExpireReason.UNFILLED)]


# Self-trade prevention


@pytest.fixture
def self_trade_book(book: OrderBook) -> OrderBook:
    """Asks: Bob 1 @ 100, then Alice 2 @ 100, then Bob 5 @ 101."""
    book.submit(oid(1), limit(BOB, SELL, 100, 1))
    book.submit(oid(2), limit(ALICE, SELL, 100, 2))
    book.submit(oid(3), limit(BOB, SELL, 101, 5))
    return book


def test_cancel_resting_removes_own_order_and_keeps_matching(self_trade_book: OrderBook) -> None:
    events = self_trade_book.submit(oid(4), limit(ALICE, BUY, 101, 4, STP.CANCEL_RESTING))
    assert events[1:] == [
        Trade(oid(1), oid(4), 100, 1),  # orders ahead of the conflict still trade
        Cancelled(oid(2), 2, CancelReason.SELF_TRADE),
        Trade(oid(3), oid(4), 101, 3),
    ]
    assert self_trade_book.depth(SELL) == [(101, 2)]


def test_cancel_incoming_expires_taker_and_keeps_resting(self_trade_book: OrderBook) -> None:
    events = self_trade_book.submit(oid(4), limit(ALICE, BUY, 101, 4, STP.CANCEL_INCOMING))
    assert events[1:] == [
        Trade(oid(1), oid(4), 100, 1),
        Expired(oid(4), 3, ExpireReason.SELF_TRADE),  # a limit order, yet it does not rest
    ]
    assert [o.order_id for o in self_trade_book.snapshot().asks] == [oid(2), oid(3)]
    assert self_trade_book.best_bid() is None


def test_cancel_both(self_trade_book: OrderBook) -> None:
    events = self_trade_book.submit(oid(4), limit(ALICE, BUY, 101, 4, STP.CANCEL_BOTH))
    assert events[1:] == [
        Trade(oid(1), oid(4), 100, 1),
        Cancelled(oid(2), 2, CancelReason.SELF_TRADE),
        Expired(oid(4), 3, ExpireReason.SELF_TRADE),
    ]
    assert [o.order_id for o in self_trade_book.snapshot().asks] == [oid(3)]


def test_own_order_beyond_the_limit_is_untouched(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, SELL, 105, 1))
    events = book.submit(oid(2), limit(ALICE, BUY, 100, 1, STP.CANCEL_BOTH))
    assert events[1:] == [Rested(oid(2), ALICE, BUY, 100, 1)]
    assert len(book) == 2


def test_taker_filled_before_reaching_own_order_cancels_nothing(book: OrderBook) -> None:
    book.submit(oid(1), limit(BOB, SELL, 100, 5))
    book.submit(oid(2), limit(ALICE, SELL, 100, 5))
    events = book.submit(oid(3), limit(ALICE, BUY, 100, 5, STP.CANCEL_BOTH))
    assert events[1:] == [Trade(oid(1), oid(3), 100, 5)]
    assert oid(2) in book


# Cancels


def test_cancel_returns_remaining_qty(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, SELL, 100, 10))
    book.submit(oid(2), limit(BOB, BUY, 100, 4))
    assert book.cancel(oid(1)) == [Cancelled(oid(1), 6, CancelReason.REQUESTED)]
    assert len(book) == 0
    assert book.best_ask() is None


def test_cancel_middle_of_queue_preserves_order(book: OrderBook) -> None:
    for n in (1, 2, 3):
        book.submit(oid(n), limit(ALICE, BUY, 100, 1))
    book.cancel(oid(2))
    assert [o.order_id for o in book.snapshot().bids] == [oid(1), oid(3)]
    book.check_invariants()


def test_cancel_non_best_level(book: OrderBook) -> None:
    for n, price in [(1, 100), (2, 99), (3, 98)]:
        book.submit(oid(n), limit(ALICE, BUY, price, 1))
    book.cancel(oid(2))
    assert book.depth(BUY) == [(100, 1), (98, 1)]
    book.check_invariants()


@pytest.mark.parametrize("setup", ["never_existed", "filled", "cancelled"])
def test_cancel_unknown_order_is_rejected(book: OrderBook, setup: str) -> None:
    if setup == "filled":
        book.submit(oid(1), limit(ALICE, SELL, 100, 1))
        book.submit(oid(2), limit(BOB, BUY, 100, 1))
    elif setup == "cancelled":
        book.submit(oid(1), limit(ALICE, SELL, 100, 1))
        book.cancel(oid(1))
    assert book.cancel(oid(1)) == [CancelRejected(oid(1), CancelRejectReason.UNKNOWN_ORDER)]


# Validation


@pytest.mark.parametrize(
    ("order_type", "price", "qty", "reason"),
    [
        (OrderType.LIMIT, 0, 1, RejectReason.INVALID_PRICE),
        (OrderType.LIMIT, -5, 1, RejectReason.INVALID_PRICE),
        (OrderType.LIMIT, None, 1, RejectReason.INVALID_PRICE),
        (OrderType.IOC, None, 1, RejectReason.INVALID_PRICE),
        (OrderType.MARKET, 100, 1, RejectReason.INVALID_PRICE),
        (OrderType.LIMIT, 100, 0, RejectReason.NON_POSITIVE_QTY),
        (OrderType.MARKET, None, -1, RejectReason.NON_POSITIVE_QTY),
        (OrderType.LIMIT, 0, 0, RejectReason.INVALID_PRICE),  # price is checked first
    ],
)
def test_invalid_orders_are_rejected_without_side_effects(
    book: OrderBook, order_type: OrderType, price: int | None, qty: int, reason: RejectReason
) -> None:
    book.submit(oid(1), limit(ALICE, SELL, 100, 1))
    before = book.snapshot()
    spec = OrderSpec(BOB, BUY, order_type, price, qty)
    assert book.submit(oid(2), spec) == [Rejected(oid(2), reason)]
    assert book.snapshot() == before


def test_duplicate_resting_id_is_rejected(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, BUY, 100, 1))
    assert book.submit(oid(1), limit(ALICE, BUY, 99, 1)) == [
        Rejected(oid(1), RejectReason.DUPLICATE_ORDER_ID)
    ]
    assert book.depth(BUY) == [(100, 1)]


def test_level_is_removed_when_emptied(book: OrderBook) -> None:
    book.submit(oid(1), limit(ALICE, SELL, 100, 1))
    book.submit(oid(2), limit(ALICE, SELL, 101, 1))
    book.submit(oid(3), limit(BOB, BUY, 100, 1))
    assert book.depth(SELL) == [(101, 1)]
    book.check_invariants()
