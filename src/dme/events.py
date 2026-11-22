"""Events emitted by the order book.

Every command produces a list of events that fully describes its effect, so the
event stream alone is enough to reconstruct the book and to compare two
implementations.

For a submit, the events are, in order:
  * ``Rejected`` alone, if the order is invalid; otherwise
  * ``Accepted``, then
  * the matching steps, best price first and oldest maker first, each either a
    ``Trade`` or, when self-trade prevention removes a resting order, a
    ``Cancelled`` with reason ``SELF_TRADE``; then
  * if any quantity is left, exactly one of: ``Rested`` (limit orders) or
    ``Expired`` (IOC and market orders, or self-trade prevention stopping the order).

For a cancel, the result is ``Cancelled`` or ``CancelRejected``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from dme.core import OrderId, OrderSpec, Price, Qty, Side, TraderId


class RejectReason(enum.Enum):
    INVALID_PRICE = "invalid_price"
    """Non-positive or missing price on a limit/IOC order, or a price on a market order."""
    NON_POSITIVE_QTY = "non_positive_qty"
    DUPLICATE_ORDER_ID = "duplicate_order_id"
    UNKNOWN_SYMBOL = "unknown_symbol"


class CancelReason(enum.Enum):
    REQUESTED = "requested"
    """The trader asked for it."""
    SELF_TRADE = "self_trade"
    """Removed by self-trade prevention when the same trader's incoming order reached it."""


class ExpireReason(enum.Enum):
    UNFILLED = "unfilled"
    """An IOC or market order ran out of matching liquidity."""
    SELF_TRADE = "self_trade"
    """Self-trade prevention stopped the incoming order."""


class CancelRejectReason(enum.Enum):
    UNKNOWN_ORDER = "unknown_order"
    """No resting order with this ID belongs to the requester. Deliberately the same
    answer whether the order never existed, is no longer resting, or belongs to someone
    else, so a cancel cannot be used to probe other traders' order IDs."""


@dataclass(frozen=True, slots=True)
class Accepted:
    order_id: OrderId
    spec: OrderSpec


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
    """The unfilled remainder of a limit order was added to the book."""

    order_id: OrderId
    trader: TraderId
    side: Side
    price: Price
    qty: Qty


@dataclass(frozen=True, slots=True)
class Cancelled:
    """A resting order was removed from the book."""

    order_id: OrderId
    qty: Qty
    """Quantity removed from the book (what was still resting)."""
    reason: CancelReason


@dataclass(frozen=True, slots=True)
class Expired:
    """The remainder of an incoming order ended without resting."""

    order_id: OrderId
    qty: Qty
    reason: ExpireReason


@dataclass(frozen=True, slots=True)
class CancelRejected:
    order_id: OrderId
    reason: CancelRejectReason


Event = Accepted | Rejected | Trade | Rested | Cancelled | Expired | CancelRejected
