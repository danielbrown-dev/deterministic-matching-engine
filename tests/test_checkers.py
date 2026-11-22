"""Tests for the test oracles themselves.

An invariant checker that never fires proves nothing. Each test here corrupts a
book or an event stream in one specific way and checks that it is caught.
These tests reach into private state on purpose.
"""

from __future__ import annotations

import pytest

from dme.audit import Audit
from dme.book import InvariantViolation, OrderBook, _Order
from dme.core import OrderId, Side
from dme.events import Accepted, Cancelled, Event, Rested, Trade

BUY, SELL = Side.BUY, Side.SELL
A, B = OrderId(1), OrderId(2)


def two_level_book() -> OrderBook:
    book = OrderBook()
    book.submit(A, BUY, 100, 5)
    book.submit(B, BUY, 99, 5)
    book.check_invariants()
    return book


def test_detects_stale_level_total() -> None:
    book = two_level_book()
    book._bids.level_at(100).total_qty += 1
    with pytest.raises(InvariantViolation, match="cached totals"):
        book.check_invariants()


def test_detects_unsorted_levels() -> None:
    book = two_level_book()
    book._bids._keys.reverse()
    with pytest.raises(InvariantViolation, match="not strictly increasing"):
        book.check_invariants()


def test_detects_order_missing_from_index() -> None:
    book = two_level_book()
    del book._orders[A]
    with pytest.raises(InvariantViolation, match="ID index"):
        book.check_invariants()


def test_detects_crossed_book() -> None:
    book = two_level_book()
    level = book._asks.level_at(100)  # rest an ask at the bid price, bypassing matching
    order = _Order(OrderId(3), SELL, 1, level)
    level.append(order)
    book._orders[order.order_id] = order
    with pytest.raises(InvariantViolation, match="crossed"):
        book.check_invariants()


def accept_and_rest(audit: Audit, order_id: OrderId, side: Side, price: int, qty: int) -> None:
    audit.record([Accepted(order_id, side, price, qty), Rested(order_id, side, price, qty)])


@pytest.mark.parametrize(
    ("events", "message"),
    [
        # Quantity disappears: 5 accepted, 2 traded, nothing rests.
        ([Accepted(B, BUY, 100, 5), Trade(A, B, 100, 2)], "lost 3"),
        # Trade at a price other than the maker's.
        ([Accepted(B, BUY, 101, 1), Trade(A, B, 101, 1)], "maker price"),
        # Trade fills more than the maker has.
        ([Accepted(B, BUY, 100, 9), Trade(A, B, 100, 9)], "out of range"),
        # Remainder rests with the wrong quantity.
        ([Accepted(B, BUY, 100, 5), Trade(A, B, 100, 2), Rested(B, BUY, 100, 2)], "unfilled"),
        # Cancel removes less than what was resting.
        ([Cancelled(A, 1)], "was resting"),
    ],
)
def test_audit_detects_bad_event_streams(events: list[Event], message: str) -> None:
    audit = Audit()
    accept_and_rest(audit, A, SELL, 100, 3)
    with pytest.raises(InvariantViolation, match=message):
        audit.record(events)


def test_audit_detects_trade_through_limit() -> None:
    audit = Audit()
    accept_and_rest(audit, A, SELL, 102, 3)
    with pytest.raises(InvariantViolation, match="limit"):
        audit.record([Accepted(B, BUY, 101, 1), Trade(A, B, 102, 1)])
