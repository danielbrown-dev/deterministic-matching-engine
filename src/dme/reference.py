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

from dme.core import (
    BookSnapshot,
    OrderId,
    OrderSpec,
    OrderType,
    Price,
    Qty,
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

_CANCELS_RESTING = (SelfTradePrevention.CANCEL_RESTING, SelfTradePrevention.CANCEL_BOTH)
_STOPS_INCOMING = (SelfTradePrevention.CANCEL_INCOMING, SelfTradePrevention.CANCEL_BOTH)


@dataclass
class _Resting:
    order_id: OrderId
    trader: TraderId
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

    def submit(self, order_id: OrderId, spec: OrderSpec) -> list[Event]:
        if not spec.has_valid_price():
            return [Rejected(order_id, RejectReason.INVALID_PRICE)]
        if spec.qty <= 0:
            return [Rejected(order_id, RejectReason.NON_POSITIVE_QTY)]
        if order_id in self:
            return [Rejected(order_id, RejectReason.DUPLICATE_ORDER_ID)]

        events: list[Event] = [Accepted(order_id, spec)]
        remaining = spec.qty
        while remaining > 0:
            makers = [o for o in self._resting if o.side is not spec.side and spec.accepts(o.price)]
            if not makers:
                break
            maker = min(makers, key=_priority)

            if maker.trader == spec.trader:
                if spec.stp in _CANCELS_RESTING:
                    self._resting.remove(maker)
                    events.append(Cancelled(maker.order_id, maker.qty, CancelReason.SELF_TRADE))
                if spec.stp in _STOPS_INCOMING:
                    events.append(Expired(order_id, remaining, ExpireReason.SELF_TRADE))
                    return events
                continue

            fill = min(remaining, maker.qty)
            events.append(Trade(maker.order_id, order_id, maker.price, fill))
            remaining -= fill
            maker.qty -= fill
            if maker.qty == 0:
                self._resting.remove(maker)

        if remaining > 0:
            if spec.order_type is OrderType.LIMIT:
                assert spec.price is not None
                resting = _Resting(
                    order_id, spec.trader, spec.side, spec.price, remaining, self._arrivals
                )
                self._resting.append(resting)
                self._arrivals += 1
                events.append(Rested(order_id, spec.trader, spec.side, spec.price, remaining))
            else:
                events.append(Expired(order_id, remaining, ExpireReason.UNFILLED))
        return events

    def cancel(self, order_id: OrderId) -> list[Event]:
        for order in self._resting:
            if order.order_id == order_id:
                self._resting.remove(order)
                return [Cancelled(order_id, order.qty, CancelReason.REQUESTED)]
        return [CancelRejected(order_id, CancelRejectReason.UNKNOWN_ORDER)]

    def __contains__(self, order_id: object) -> bool:
        return any(o.order_id == order_id for o in self._resting)

    def snapshot(self) -> BookSnapshot:
        def side_view(side: Side) -> tuple[RestingOrder, ...]:
            orders = sorted((o for o in self._resting if o.side is side), key=_priority)
            return tuple(RestingOrder(o.order_id, o.trader, o.side, o.price, o.qty) for o in orders)

        return BookSnapshot(bids=side_view(Side.BUY), asks=side_view(Side.SELL))
