"""Example-based tests: each pins down one rule of the matching semantics."""

from __future__ import annotations

import pytest

from dme.book import OrderBook
from dme.core import OrderId, RestingOrder, Side
from dme.events import (
    Accepted,
    Cancelled,
    CancelRejected,
    CancelRejectReason,
    Rejected,
    RejectReason,
    Rested,
    Trade,
)

BUY, SELL = Side.BUY, Side.SELL


def oid(n: int) -> OrderId:
    return OrderId(n)


@pytest.fixture
def book() -> OrderBook:
    return OrderBook()


def test_order_rests_on_empty_book(book: OrderBook) -> None:
    assert book.submit(oid(1), BUY, 100, 5) == [
        Accepted(oid(1), BUY, 100, 5),
        Rested(oid(1), BUY, 100, 5),
    ]
    assert book.best_bid() == 100
    assert book.best_ask() is None
    assert book.depth(BUY) == [(100, 5)]


def test_non_crossing_orders_both_rest(book: OrderBook) -> None:
    book.submit(oid(1), BUY, 99, 5)
    book.submit(oid(2), SELL, 101, 5)
    assert (book.best_bid(), book.best_ask()) == (99, 101)
    assert len(book) == 2


def test_equal_prices_cross(book: OrderBook) -> None:
    book.submit(oid(1), SELL, 100, 5)
    assert book.submit(oid(2), BUY, 100, 5) == [
        Accepted(oid(2), BUY, 100, 5),
        Trade(maker_id=oid(1), taker_id=oid(2), price=100, qty=5),
    ]
    assert len(book) == 0


def test_partial_fill_of_maker_leaves_remainder_at_front(book: OrderBook) -> None:
    book.submit(oid(1), SELL, 100, 10)
    book.submit(oid(2), SELL, 100, 10)
    book.submit(oid(3), BUY, 100, 4)
    assert book.snapshot().asks == (
        RestingOrder(oid(1), SELL, 100, 6),
        RestingOrder(oid(2), SELL, 100, 10),
    )
    assert book.depth(SELL) == [(100, 16)]


def test_partial_fill_of_taker_rests_remainder(book: OrderBook) -> None:
    book.submit(oid(1), SELL, 100, 3)
    assert book.submit(oid(2), BUY, 100, 10) == [
        Accepted(oid(2), BUY, 100, 10),
        Trade(oid(1), oid(2), 100, 3),
        Rested(oid(2), BUY, 100, 7),
    ]
    assert (book.best_bid(), book.best_ask()) == (100, None)


def test_time_priority_within_a_level(book: OrderBook) -> None:
    book.submit(oid(1), BUY, 100, 2)
    book.submit(oid(2), BUY, 100, 2)
    book.submit(oid(3), BUY, 100, 2)
    events = book.submit(oid(4), SELL, 100, 5)
    assert [e for e in events if isinstance(e, Trade)] == [
        Trade(oid(1), oid(4), 100, 2),
        Trade(oid(2), oid(4), 100, 2),
        Trade(oid(3), oid(4), 100, 1),
    ]


def test_price_priority_beats_time_priority(book: OrderBook) -> None:
    book.submit(oid(1), SELL, 102, 1)  # older but worse
    book.submit(oid(2), SELL, 101, 1)
    events = book.submit(oid(3), BUY, 102, 1)
    assert Trade(oid(2), oid(3), 101, 1) in events
    assert book.best_ask() == 102


def test_sweep_trades_at_each_makers_price(book: OrderBook) -> None:
    book.submit(oid(1), SELL, 101, 1)
    book.submit(oid(2), SELL, 102, 1)
    book.submit(oid(3), SELL, 103, 1)
    book.submit(oid(4), SELL, 104, 1)
    events = book.submit(oid(5), BUY, 103, 10)
    assert events == [
        Accepted(oid(5), BUY, 103, 10),
        Trade(oid(1), oid(5), 101, 1),
        Trade(oid(2), oid(5), 102, 1),
        Trade(oid(3), oid(5), 103, 1),
        Rested(oid(5), BUY, 103, 7),
    ]
    assert (book.best_bid(), book.best_ask()) == (103, 104)


def test_sell_taker_sweeps_bids_from_highest(book: OrderBook) -> None:
    book.submit(oid(1), BUY, 98, 1)
    book.submit(oid(2), BUY, 100, 1)
    book.submit(oid(3), BUY, 99, 1)
    events = book.submit(oid(4), SELL, 99, 3)
    assert [e for e in events if isinstance(e, Trade)] == [
        Trade(oid(2), oid(4), 100, 1),
        Trade(oid(3), oid(4), 99, 1),
    ]
    assert (book.best_bid(), book.best_ask()) == (98, 99)


def test_cancel_returns_remaining_qty(book: OrderBook) -> None:
    book.submit(oid(1), SELL, 100, 10)
    book.submit(oid(2), BUY, 100, 4)
    assert book.cancel(oid(1)) == [Cancelled(oid(1), 6)]
    assert len(book) == 0
    assert book.best_ask() is None


def test_cancel_middle_of_queue_preserves_order(book: OrderBook) -> None:
    for n in (1, 2, 3):
        book.submit(oid(n), BUY, 100, 1)
    book.cancel(oid(2))
    assert [o.order_id for o in book.snapshot().bids] == [oid(1), oid(3)]
    book.check_invariants()


def test_cancel_non_best_level(book: OrderBook) -> None:
    book.submit(oid(1), BUY, 100, 1)
    book.submit(oid(2), BUY, 99, 1)
    book.submit(oid(3), BUY, 98, 1)
    book.cancel(oid(2))
    assert book.depth(BUY) == [(100, 1), (98, 1)]
    book.check_invariants()


@pytest.mark.parametrize("setup", ["never_existed", "filled", "cancelled"])
def test_cancel_unknown_order_is_rejected(book: OrderBook, setup: str) -> None:
    if setup == "filled":
        book.submit(oid(1), SELL, 100, 1)
        book.submit(oid(2), BUY, 100, 1)
    elif setup == "cancelled":
        book.submit(oid(1), SELL, 100, 1)
        book.cancel(oid(1))
    assert book.cancel(oid(1)) == [CancelRejected(oid(1), CancelRejectReason.UNKNOWN_ORDER)]


@pytest.mark.parametrize(
    ("price", "qty", "reason"),
    [
        (0, 1, RejectReason.NON_POSITIVE_PRICE),
        (-5, 1, RejectReason.NON_POSITIVE_PRICE),
        (100, 0, RejectReason.NON_POSITIVE_QTY),
        (100, -1, RejectReason.NON_POSITIVE_QTY),
        (0, 0, RejectReason.NON_POSITIVE_PRICE),  # price is checked first
    ],
)
def test_invalid_orders_are_rejected_without_side_effects(
    book: OrderBook, price: int, qty: int, reason: RejectReason
) -> None:
    book.submit(oid(1), SELL, 100, 1)
    before = book.snapshot()
    assert book.submit(oid(2), BUY, price, qty) == [Rejected(oid(2), reason)]
    assert book.snapshot() == before


def test_duplicate_resting_id_is_rejected(book: OrderBook) -> None:
    book.submit(oid(1), BUY, 100, 1)
    assert book.submit(oid(1), BUY, 99, 1) == [Rejected(oid(1), RejectReason.DUPLICATE_ORDER_ID)]
    assert book.depth(BUY) == [(100, 1)]


def test_id_of_filled_order_can_be_reused(book: OrderBook) -> None:
    """The book only guards resting IDs; global uniqueness belongs to the engine."""
    book.submit(oid(1), SELL, 100, 1)
    book.submit(oid(2), BUY, 100, 1)
    assert book.submit(oid(1), SELL, 100, 1)[0] == Accepted(oid(1), SELL, 100, 1)


def test_level_is_removed_when_emptied(book: OrderBook) -> None:
    book.submit(oid(1), SELL, 100, 1)
    book.submit(oid(2), SELL, 101, 1)
    book.submit(oid(3), BUY, 100, 1)
    assert book.depth(SELL) == [(101, 1)]
    book.check_invariants()
