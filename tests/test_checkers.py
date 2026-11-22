"""Tests for the test oracles themselves.

An invariant checker that never fires proves nothing. Each test here corrupts a
book or an event stream in one specific way and checks that it is caught.
These tests reach into private state on purpose.
"""

from __future__ import annotations

import pytest

from dme.audit import Audit
from dme.book import InvariantViolation, OrderBook, _Order
from dme.core import OrderId, OrderSpec, SelfTradePrevention, Side, TraderId
from dme.events import (
    Accepted,
    Cancelled,
    CancelReason,
    CancelRejected,
    CancelRejectReason,
    Event,
    Expired,
    ExpireReason,
    Rested,
    Trade,
)

BUY, SELL = Side.BUY, Side.SELL
ALICE, BOB = TraderId(1), TraderId(2)
A, B = OrderId(1), OrderId(2)
STP = SelfTradePrevention


# OrderBook.check_invariants


def two_level_book() -> OrderBook:
    book = OrderBook()
    book.submit(A, OrderSpec.limit(ALICE, BUY, 100, 5))
    book.submit(B, OrderSpec.limit(ALICE, BUY, 99, 5))
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
    order = _Order(OrderId(3), BOB, SELL, 1, level)
    level.append(order)
    book._orders[order.order_id] = order
    with pytest.raises(InvariantViolation, match="crossed"):
        book.check_invariants()


# Audit: submits


def audit_with_resting_ask(trader: TraderId = ALICE, price: int = 100, qty: int = 3) -> Audit:
    audit = Audit()
    spec = OrderSpec.limit(trader, SELL, price, qty)
    audit.submitted([Accepted(A, spec), Rested(A, trader, SELL, price, qty)])
    return audit


def bob_buys(price: int, qty: int) -> Accepted:
    return Accepted(B, OrderSpec.limit(BOB, BUY, price, qty))


@pytest.mark.parametrize(
    ("events", "message"),
    [
        # Quantity disappears: 5 accepted, 2 traded, nothing rests.
        ([bob_buys(100, 5), Trade(A, B, 100, 2)], "lost 3"),
        # Trade at a price other than the maker's.
        ([bob_buys(101, 1), Trade(A, B, 101, 1)], "maker price"),
        # Trade fills more than the maker has.
        ([bob_buys(100, 9), Trade(A, B, 100, 9)], "out of range"),
        # Remainder rests with the wrong quantity.
        ([bob_buys(100, 5), Trade(A, B, 100, 2), Rested(B, BOB, BUY, 100, 2)], "unfilled"),
        # A limit order expires instead of resting.
        ([bob_buys(99, 5), Expired(B, 5, ExpireReason.UNFILLED)], "instead of resting"),
        # An IOC order rests.
        (
            [Accepted(B, OrderSpec.ioc(BOB, BUY, 99, 5)), Rested(B, BOB, BUY, 99, 5)],
            "IOC order rested",
        ),
        # Something happens after the order finished.
        ([bob_buys(99, 1), Rested(B, BOB, BUY, 99, 1), Trade(A, B, 100, 1)], "finished"),
    ],
)
def test_audit_detects_bad_submits(events: list[Event], message: str) -> None:
    audit = audit_with_resting_ask()
    with pytest.raises(InvariantViolation, match=message):
        audit.submitted(events)


def test_audit_detects_trade_through_limit() -> None:
    audit = audit_with_resting_ask(price=102)
    with pytest.raises(InvariantViolation, match="limit"):
        audit.submitted([bob_buys(101, 1), Trade(A, B, 102, 1)])


# Audit: self-trade prevention


def alice_buys(stp: STP, qty: int = 1) -> Accepted:
    return Accepted(B, OrderSpec.limit(ALICE, BUY, 100, qty, stp))


@pytest.mark.parametrize(
    ("events", "message"),
    [
        # Alice trades with herself.
        ([alice_buys(STP.CANCEL_RESTING), Trade(A, B, 100, 1)], "self-trade by trader"),
        # CANCEL_INCOMING must not cancel the resting order.
        (
            [
                alice_buys(STP.CANCEL_INCOMING),
                Cancelled(A, 3, CancelReason.SELF_TRADE),
                Expired(B, 1, ExpireReason.SELF_TRADE),
            ],
            "must not cancel the resting",
        ),
        # CANCEL_RESTING must not stop the incoming order.
        ([alice_buys(STP.CANCEL_RESTING), Expired(B, 1, ExpireReason.SELF_TRADE)], "must not stop"),
        # CANCEL_BOTH must cancel the resting order before expiring.
        ([alice_buys(STP.CANCEL_BOTH), Expired(B, 1, ExpireReason.SELF_TRADE)], "without a self"),
        # A self-trade cancel must remove the whole resting order.
        (
            [alice_buys(STP.CANCEL_RESTING), Cancelled(A, 1, CancelReason.SELF_TRADE)],
            "removed 1, 3",
        ),
    ],
)
def test_audit_detects_self_trade_prevention_errors(events: list[Event], message: str) -> None:
    audit = audit_with_resting_ask(trader=ALICE)
    with pytest.raises(InvariantViolation, match=message):
        audit.submitted(events)


def test_audit_detects_self_trade_cancel_of_another_traders_order() -> None:
    audit = audit_with_resting_ask(trader=BOB)
    with pytest.raises(InvariantViolation, match="another trader"):
        audit.submitted([alice_buys(STP.CANCEL_RESTING), Cancelled(A, 3, CancelReason.SELF_TRADE)])


def test_audit_detects_self_trade_expiry_with_no_conflict() -> None:
    audit = audit_with_resting_ask(trader=BOB)
    with pytest.raises(InvariantViolation, match="no conflicting order"):
        audit.submitted([alice_buys(STP.CANCEL_INCOMING), Expired(B, 1, ExpireReason.SELF_TRADE)])


# Audit: cancels


def test_audit_detects_cancel_by_non_owner() -> None:
    audit = audit_with_resting_ask(trader=ALICE)
    with pytest.raises(InvariantViolation, match="may not cancel"):
        audit.cancel_requested(A, BOB, [Cancelled(A, 3, CancelReason.REQUESTED)])


def test_audit_detects_rejected_valid_cancel() -> None:
    audit = audit_with_resting_ask(trader=ALICE)
    with pytest.raises(InvariantViolation, match="rejected a valid cancel"):
        audit.cancel_requested(A, ALICE, [CancelRejected(A, CancelRejectReason.UNKNOWN_ORDER)])


def test_audit_detects_partial_cancel() -> None:
    audit = audit_with_resting_ask(qty=3)
    with pytest.raises(InvariantViolation, match="was resting"):
        audit.cancel_requested(A, None, [Cancelled(A, 1, CancelReason.REQUESTED)])
