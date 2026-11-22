"""Checks on the event stream that hold for any correct order book.

The reference model catches *wrong answers*, but only if the model itself is right.
The audit is a second, independent oracle: it never looks inside a book, it only
replays the events a book emitted and checks that they obey the rules every
correct book must follow:

  * Quantity is conserved: for every order, filled + resting + cancelled equals
    the accepted quantity. Nothing is created or lost.
  * Trades only happen against resting orders, at the maker's resting price, and
    never at a price worse than the taker's limit.
  * Only the incoming order can be the taker, and any unfilled remainder rests.
  * A cancel succeeds exactly when the order is resting, and removes all of it.
  * Every accepted order ends in exactly one state: fully filled, cancelled, or
    still resting.

The simulator (M3) runs the audit after every step.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from dme.core import InvariantViolation, OrderId, Price, Qty, Side
from dme.events import (
    Accepted,
    Cancelled,
    CancelRejected,
    Event,
    Rejected,
    RejectReason,
    Rested,
    Trade,
)


@dataclass
class _Resting:
    side: Side
    price: Price
    qty: Qty


class Audit:
    """Replays events, raising ``InvariantViolation`` on the first rule broken."""

    def __init__(self) -> None:
        self._resting: dict[OrderId, _Resting] = {}

    def resting(self) -> dict[OrderId, tuple[Side, Price, Qty]]:
        """What should be on the book, according to the events seen so far."""
        return {oid: (r.side, r.price, r.qty) for oid, r in self._resting.items()}

    def record(self, events: Sequence[Event]) -> None:
        """Check the events produced by one command."""
        if not events:
            raise InvariantViolation("a command produced no events")
        first = events[0]
        if isinstance(first, Accepted):
            self._accepted(first, events[1:])
        elif len(events) != 1:
            raise InvariantViolation(f"{type(first).__name__} must be the only event: {events}")
        elif isinstance(first, Rejected):
            # Price and quantity are validated before the ID, so a resting ID may be
            # rejected for another reason. A duplicate rejection, though, needs a
            # resting order with that ID. (Accepting a resting ID is caught in _accepted.)
            is_duplicate = first.reason is RejectReason.DUPLICATE_ORDER_ID
            if is_duplicate and first.order_id not in self._resting:
                raise InvariantViolation(f"rejected as duplicate but not resting: {first}")
        elif isinstance(first, Cancelled):
            resting = self._resting.pop(first.order_id, None)
            if resting is None:
                raise InvariantViolation(f"cancelled an order that was not resting: {first}")
            if first.qty != resting.qty:
                raise InvariantViolation(f"cancel removed {first.qty}, {resting.qty} was resting")
        elif isinstance(first, CancelRejected):
            if first.order_id in self._resting:
                raise InvariantViolation(f"rejected a cancel for a resting order: {first}")
        else:
            raise InvariantViolation(f"a command cannot start with {first}")

    def _accepted(self, accepted: Accepted, rest: Sequence[Event]) -> None:
        taker = accepted.order_id
        if taker in self._resting:
            raise InvariantViolation(f"accepted an ID that is already resting: {accepted}")
        if accepted.price <= 0 or accepted.qty <= 0:
            raise InvariantViolation(f"accepted an invalid order: {accepted}")
        remaining = accepted.qty
        rested = False
        for event in rest:
            if rested:
                raise InvariantViolation(f"event after Rested: {event}")
            if isinstance(event, Trade):
                remaining = self._trade(accepted, event, remaining)
            elif isinstance(event, Rested):
                if (event.order_id, event.side, event.price) != (
                    taker,
                    accepted.side,
                    accepted.price,
                ):
                    raise InvariantViolation(f"Rested does not match the order: {event}")
                if event.qty != remaining:
                    raise InvariantViolation(f"rested {event.qty} but {remaining} was unfilled")
                self._resting[taker] = _Resting(accepted.side, accepted.price, remaining)
                rested = True
            else:
                raise InvariantViolation(f"unexpected event during a submit: {event}")
        if remaining > 0 and not rested:
            raise InvariantViolation(f"order {taker} lost {remaining} unfilled lots")

    def _trade(self, accepted: Accepted, trade: Trade, remaining: Qty) -> Qty:
        if trade.taker_id != accepted.order_id:
            raise InvariantViolation(f"trade taker is not the incoming order: {trade}")
        maker = self._resting.get(trade.maker_id)
        if maker is None:
            raise InvariantViolation(f"trade against an order that is not resting: {trade}")
        if maker.side is accepted.side:
            raise InvariantViolation(f"trade between two {maker.side.name} orders: {trade}")
        if trade.price != maker.price:
            raise InvariantViolation(f"trade price {trade.price} != maker price {maker.price}")
        limit_ok = (
            trade.price <= accepted.price
            if accepted.side is Side.BUY
            else trade.price >= accepted.price
        )
        if not limit_ok:
            raise InvariantViolation(f"trade through the taker's limit {accepted.price}: {trade}")
        if not 0 < trade.qty <= min(remaining, maker.qty):
            raise InvariantViolation(f"trade qty {trade.qty} out of range: {trade}")
        maker.qty -= trade.qty
        if maker.qty == 0:
            del self._resting[trade.maker_id]
        return remaining - trade.qty
