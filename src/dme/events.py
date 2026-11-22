"""Events emitted by the order book.

Every command produces a list of events that fully describes its effect, so the
event stream alone is enough to reconstruct the book and to compare two
implementations.

For a submit, the events are, in order:
  * ``Rejected`` alone, if the order is invalid; otherwise
  * ``Accepted``, then
  * zero or more ``Trade`` events, best price first and oldest maker first, then
  * ``Rested`` if any quantity remains after matching.

For a cancel, the result is ``Cancelled`` or ``CancelRejected``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from dme.core import OrderId, Price, Qty, Side


class RejectReason(enum.Enum):
    NON_POSITIVE_PRICE = "non_positive_price"
    NON_POSITIVE_QTY = "non_positive_qty"
    DUPLICATE_ORDER_ID = "duplicate_order_id"


class CancelRejectReason(enum.Enum):
    UNKNOWN_ORDER = "unknown_order"
    """No resting order has this ID: it never existed, was fully filled, or was cancelled."""


@dataclass(frozen=True, slots=True)
class Accepted:
    order_id: OrderId
    side: Side
    price: Price
    qty: Qty


@dataclass(frozen=True, slots=True)
class Rejected:
    order_id: OrderId
    reason: RejectReason


@dataclass(frozen=True, slots=True)
class Trade:
    """An execution between a resting maker and an incoming taker.

    The trade price is always the maker's price: the taker gets any price improvement.
    """

    maker_id: OrderId
    taker_id: OrderId
    price: Price
    qty: Qty


@dataclass(frozen=True, slots=True)
class Rested:
    """The unfilled remainder of an order was added to the book."""

    order_id: OrderId
    side: Side
    price: Price
    qty: Qty


@dataclass(frozen=True, slots=True)
class Cancelled:
    order_id: OrderId
    qty: Qty
    """Quantity removed from the book (what was still resting)."""


@dataclass(frozen=True, slots=True)
class CancelRejected:
    order_id: OrderId
    reason: CancelRejectReason


Event = Accepted | Rejected | Trade | Rested | Cancelled | CancelRejected
