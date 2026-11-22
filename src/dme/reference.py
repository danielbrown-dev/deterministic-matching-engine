"""A deliberately naive order book used as a test oracle.

This model is written to be obviously correct, not fast: resting orders live in a
flat list, and every operation scans it. Price-time priority is stated directly as
a sort key (best price first, then arrival order) instead of being encoded in data
structures.

The real ``OrderBook`` must produce exactly the same events and snapshots as this
model for every sequence of commands. Keep this file simple: if it ever needs a
clever data structure, it has stopped being a useful oracle.
"""

from __future__ import annotations

from dataclasses import dataclass

from dme.core import BookSnapshot, OrderId, Price, Qty, RestingOrder, Side
from dme.events import (
    Accepted,
    Cancelled,
    CancelRejected,
    CancelRejectReason,
    Event,
    Rejected,
    RejectReason,
    Rested,
    Trade,
)


@dataclass
class _Resting:
    order_id: OrderId
    side: Side
    price: Price
    qty: Qty
    arrival: int


def _priority(order: _Resting) -> tuple[int, int]:
    """Sort key: better price first, then earlier arrival."""
    price_rank = -order.price if order.side is Side.BUY else order.price
    return (price_rank, order.arrival)


class ReferenceBook:
    def __init__(self) -> None:
        self._resting: list[_Resting] = []
        self._arrivals = 0

    def submit(self, order_id: OrderId, side: Side, price: Price, qty: Qty) -> list[Event]:
        if price <= 0:
            return [Rejected(order_id, RejectReason.NON_POSITIVE_PRICE)]
        if qty <= 0:
            return [Rejected(order_id, RejectReason.NON_POSITIVE_QTY)]
        if any(o.order_id == order_id for o in self._resting):
            return [Rejected(order_id, RejectReason.DUPLICATE_ORDER_ID)]

        events: list[Event] = [Accepted(order_id, side, price, qty)]
        remaining = qty
        while remaining > 0:
            makers = [
                o for o in self._resting if o.side is not side and _acceptable(side, price, o)
            ]
            if not makers:
                break
            maker = min(makers, key=_priority)
            fill = min(remaining, maker.qty)
            events.append(Trade(maker.order_id, order_id, maker.price, fill))
            remaining -= fill
            maker.qty -= fill
            if maker.qty == 0:
                self._resting.remove(maker)

        if remaining > 0:
            self._resting.append(_Resting(order_id, side, price, remaining, self._arrivals))
            self._arrivals += 1
            events.append(Rested(order_id, side, price, remaining))
        return events

    def cancel(self, order_id: OrderId) -> list[Event]:
        for order in self._resting:
            if order.order_id == order_id:
                self._resting.remove(order)
                return [Cancelled(order_id, order.qty)]
        return [CancelRejected(order_id, CancelRejectReason.UNKNOWN_ORDER)]

    def snapshot(self) -> BookSnapshot:
        def side_view(side: Side) -> tuple[RestingOrder, ...]:
            orders = sorted((o for o in self._resting if o.side is side), key=_priority)
            return tuple(RestingOrder(o.order_id, o.side, o.price, o.qty) for o in orders)

        return BookSnapshot(bids=side_view(Side.BUY), asks=side_view(Side.SELL))


def _acceptable(taker_side: Side, limit: Price, maker: _Resting) -> bool:
    if taker_side is Side.BUY:
        return maker.price <= limit
    return maker.price >= limit
