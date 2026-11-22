"""Checks on the event stream that hold for any correct order book or engine.

The reference model catches *wrong answers*, but only if the model itself is right.
The audit is a second, independent oracle: it never looks inside a book, it only
replays the events a book emitted and checks that they obey the rules every
correct book must follow:

  * Quantity is conserved: for every order, filled + resting + cancelled + expired
    equals the accepted quantity. Nothing is created or lost.
  * Trades only happen against resting orders, at the maker's resting price, never
    at a price the taker did not accept, and never between two orders of one trader.
  * Self-trade prevention cancels exactly what the incoming order's mode says, and
    only orders the incoming order actually reached.
  * Only limit orders rest; IOC and market remainders expire.
  * A cancel succeeds exactly when the order is resting and the requester owns it,
    and removes all of it.

The simulator (M3) runs the audit after every step.
"""

from __future__ import annotations

from collections.abc import Sequence

from dme.core import (
    InvariantViolation,
    OrderId,
    OrderType,
    Qty,
    RestingOrder,
    SelfTradePrevention,
    TraderId,
)
from dme.events import (
    Accepted,
    Cancelled,
    CancelReason,
    CancelRejected,
    Event,
    Expired,
    ExpireReason,
    Rejected,
    RejectReason,
    Rested,
    Trade,
)

_CANCELS_RESTING = (SelfTradePrevention.CANCEL_RESTING, SelfTradePrevention.CANCEL_BOTH)
_STOPS_INCOMING = (SelfTradePrevention.CANCEL_INCOMING, SelfTradePrevention.CANCEL_BOTH)


class Audit:
    """Replays events, raising ``InvariantViolation`` on the first rule broken.

    Order IDs must be unique across everything audited by one instance, so a single
    audit can follow all books of an engine.
    """

    def __init__(self) -> None:
        self._resting: dict[OrderId, RestingOrder] = {}

    def resting(self) -> dict[OrderId, RestingOrder]:
        """What should be on the book(s), according to the events seen so far."""
        return dict(self._resting)

    # Commands

    def submitted(self, events: Sequence[Event]) -> None:
        """Check the events produced by one submit."""
        if not events:
            raise InvariantViolation("a submit produced no events")
        first = events[0]
        if isinstance(first, Rejected):
            if len(events) != 1:
                raise InvariantViolation(f"Rejected must be the only event: {events}")
            if (
                first.reason is RejectReason.DUPLICATE_ORDER_ID
                and first.order_id not in self._resting
            ):
                raise InvariantViolation(f"rejected as duplicate but not resting: {first}")
        elif isinstance(first, Accepted):
            _Submit(self._resting, first).replay(events[1:])
        else:
            raise InvariantViolation(f"a submit cannot start with {first}")

    def cancel_requested(
        self, order_id: OrderId, requester: TraderId | None, events: Sequence[Event]
    ) -> None:
        """Check the events produced by one cancel request.

        ``requester`` is None when ownership is not checked (a bare order book).
        """
        if len(events) != 1:
            raise InvariantViolation(f"a cancel must produce exactly one event: {events}")
        (event,) = events
        resting = self._resting.get(order_id)
        allowed = resting is not None and requester in (None, resting.trader)
        if isinstance(event, Cancelled) and event.order_id == order_id:
            if not allowed or resting is None:
                raise InvariantViolation(
                    f"cancelled an order the requester may not cancel: {event}"
                )
            if event.reason is not CancelReason.REQUESTED:
                raise InvariantViolation(f"requested cancel has reason {event.reason}")
            if event.qty != resting.qty:
                raise InvariantViolation(f"cancel removed {event.qty}, {resting.qty} was resting")
            del self._resting[order_id]
        elif isinstance(event, CancelRejected) and event.order_id == order_id:
            if allowed:
                raise InvariantViolation(f"rejected a valid cancel: {event}")
        else:
            raise InvariantViolation(f"unexpected event for a cancel of {order_id}: {event}")


class _Submit:
    """Replays the events after ``Accepted`` for one incoming order."""

    def __init__(self, resting: dict[OrderId, RestingOrder], accepted: Accepted) -> None:
        self.resting = resting
        self.taker = accepted.order_id
        self.spec = accepted.spec
        self.remaining: Qty = accepted.spec.qty
        if self.taker in resting:
            raise InvariantViolation(f"accepted an ID that is already resting: {accepted}")
        if not self.spec.has_valid_price() or self.spec.qty <= 0:
            raise InvariantViolation(f"accepted an invalid order: {accepted}")

    def replay(self, events: Sequence[Event]) -> None:
        finished = False
        previous: Event | None = None
        for event in events:
            if finished:
                raise InvariantViolation(f"event after the order finished: {event}")
            if isinstance(event, Trade):
                self._trade(event)
            elif isinstance(event, Cancelled):
                self._self_trade_cancel(event)
            elif isinstance(event, Rested):
                self._rested(event)
                finished = True
            elif isinstance(event, Expired):
                self._expired(event, previous)
                finished = True
            else:
                raise InvariantViolation(f"unexpected event during a submit: {event}")
            previous = event
        if self.remaining > 0 and not finished:
            raise InvariantViolation(f"order {self.taker} lost {self.remaining} unfilled lots")

    def _reachable_maker(self, maker_id: OrderId, what: str) -> RestingOrder:
        maker = self.resting.get(maker_id)
        if maker is None:
            raise InvariantViolation(f"{what} against order {maker_id}, which is not resting")
        if maker.side is self.spec.side:
            raise InvariantViolation(f"{what} between two {maker.side.name} orders")
        if not self.spec.accepts(maker.price):
            raise InvariantViolation(
                f"{what} at {maker.price}, outside the taker's limit {self.spec.price}"
            )
        return maker

    def _trade(self, trade: Trade) -> None:
        if trade.taker_id != self.taker:
            raise InvariantViolation(f"trade taker is not the incoming order: {trade}")
        maker = self._reachable_maker(trade.maker_id, "trade")
        if maker.trader == self.spec.trader:
            raise InvariantViolation(f"self-trade by trader {maker.trader}: {trade}")
        if trade.price != maker.price:
            raise InvariantViolation(f"trade price {trade.price} != maker price {maker.price}")
        if not 0 < trade.qty <= min(self.remaining, maker.qty):
            raise InvariantViolation(f"trade qty {trade.qty} out of range: {trade}")
        self.remaining -= trade.qty
        if trade.qty == maker.qty:
            del self.resting[trade.maker_id]
        else:
            self.resting[trade.maker_id] = RestingOrder(
                maker.order_id, maker.trader, maker.side, maker.price, maker.qty - trade.qty
            )

    def _self_trade_cancel(self, event: Cancelled) -> None:
        maker = self._reachable_maker(event.order_id, "self-trade cancel")
        if event.reason is not CancelReason.SELF_TRADE:
            raise InvariantViolation(
                f"resting order cancelled during a submit with reason {event.reason}"
            )
        if maker.trader != self.spec.trader:
            raise InvariantViolation(f"self-trade cancel of another trader's order: {event}")
        if self.spec.stp not in _CANCELS_RESTING:
            raise InvariantViolation(f"{self.spec.stp} must not cancel the resting order: {event}")
        if event.qty != maker.qty:
            raise InvariantViolation(
                f"self-trade cancel removed {event.qty}, {maker.qty} was resting"
            )
        del self.resting[event.order_id]

    def _rested(self, event: Rested) -> None:
        if self.spec.order_type is not OrderType.LIMIT:
            raise InvariantViolation(f"a {self.spec.order_type.name} order rested: {event}")
        expected = (self.taker, self.spec.trader, self.spec.side, self.spec.price)
        if (event.order_id, event.trader, event.side, event.price) != expected:
            raise InvariantViolation(f"Rested does not match the order: {event}")
        if event.qty != self.remaining or event.qty <= 0:
            raise InvariantViolation(f"rested {event.qty} but {self.remaining} was unfilled")
        self.resting[self.taker] = RestingOrder(
            event.order_id, event.trader, event.side, event.price, event.qty
        )

    def _expired(self, event: Expired, previous: Event | None) -> None:
        if event.order_id != self.taker:
            raise InvariantViolation(f"Expired is not for the incoming order: {event}")
        if event.qty != self.remaining or event.qty <= 0:
            raise InvariantViolation(f"expired {event.qty} but {self.remaining} was unfilled")
        if event.reason is ExpireReason.UNFILLED:
            if self.spec.order_type is OrderType.LIMIT:
                raise InvariantViolation(f"a limit order expired instead of resting: {event}")
            return
        if self.spec.stp not in _STOPS_INCOMING:
            raise InvariantViolation(f"{self.spec.stp} must not stop the incoming order: {event}")
        if self.spec.stp is SelfTradePrevention.CANCEL_BOTH:
            # The conflicting resting order was just cancelled.
            ok = isinstance(previous, Cancelled) and previous.reason is CancelReason.SELF_TRADE
            if not ok:
                raise InvariantViolation(f"cancel-both expiry without a self-trade cancel: {event}")
        else:
            # The conflicting resting order is still there, and the taker reached it.
            # (With one audit across several books this check cannot tell symbols
            # apart, so it can miss a bug but never reports a false one.)
            reachable = any(
                order.trader == self.spec.trader
                and order.side is not self.spec.side
                and self.spec.accepts(order.price)
                for order in self.resting.values()
            )
            if not reachable:
                raise InvariantViolation(f"self-trade expiry with no conflicting order: {event}")
