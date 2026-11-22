"""Core value types shared by the order book, the engine, the reference model and the
simulator.

Prices and quantities are integers: prices in ticks, quantities in lots. Floats are
never used, so arithmetic is exact and results are reproducible across platforms.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import NewType

OrderId = NewType("OrderId", int)
TraderId = NewType("TraderId", int)
Symbol = NewType("Symbol", str)
Price = int
"""A price in integer ticks. Always positive for a valid order."""
Qty = int
"""A quantity in integer lots. Always positive for a valid order."""


class InvariantViolation(Exception):
    """A correctness invariant does not hold. Always indicates a bug."""


class Side(enum.Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(enum.Enum):
    LIMIT = "limit"
    """Match up to the limit price, then rest any remainder on the book."""
    IOC = "ioc"
    """Immediate-or-cancel: match up to the limit price, then expire any remainder."""
    MARKET = "market"
    """Match at any price, then expire any remainder. Has no price."""


class SelfTradePrevention(enum.Enum):
    """What to do when an incoming order would trade with a resting order of the same
    trader. The incoming order's setting decides; the resting order's is not stored.

    Only the conflicting resting order is affected: matching against other traders'
    orders continues as normal until the conflict is reached.
    """

    CANCEL_RESTING = "cancel_resting"
    """Cancel the resting order and keep matching. The default, as on CME Globex: the
    incoming order is the trader's latest intent, the resting one is likely stale."""
    CANCEL_INCOMING = "cancel_incoming"
    """Expire the rest of the incoming order. The resting order stays on the book."""
    CANCEL_BOTH = "cancel_both"
    """Cancel the resting order and expire the rest of the incoming order."""


@dataclass(frozen=True, slots=True)
class OrderSpec:
    """What a trader asks for when submitting an order."""

    trader: TraderId
    side: Side
    order_type: OrderType
    price: Price | None
    """Required for LIMIT and IOC orders; must be None for MARKET orders."""
    qty: Qty
    stp: SelfTradePrevention = SelfTradePrevention.CANCEL_RESTING

    @classmethod
    def limit(
        cls,
        trader: TraderId,
        side: Side,
        price: Price,
        qty: Qty,
        stp: SelfTradePrevention = SelfTradePrevention.CANCEL_RESTING,
    ) -> OrderSpec:
        return cls(trader, side, OrderType.LIMIT, price, qty, stp)

    @classmethod
    def ioc(
        cls,
        trader: TraderId,
        side: Side,
        price: Price,
        qty: Qty,
        stp: SelfTradePrevention = SelfTradePrevention.CANCEL_RESTING,
    ) -> OrderSpec:
        return cls(trader, side, OrderType.IOC, price, qty, stp)

    @classmethod
    def market(
        cls,
        trader: TraderId,
        side: Side,
        qty: Qty,
        stp: SelfTradePrevention = SelfTradePrevention.CANCEL_RESTING,
    ) -> OrderSpec:
        return cls(trader, side, OrderType.MARKET, None, qty, stp)

    def has_valid_price(self) -> bool:
        if self.order_type is OrderType.MARKET:
            return self.price is None
        return self.price is not None and self.price > 0

    def accepts(self, price: Price) -> bool:
        """Whether this order may trade at ``price``."""
        if self.price is None:
            return True  # market order
        if self.side is Side.BUY:
            return price <= self.price
        return price >= self.price


@dataclass(frozen=True, slots=True)
class RestingOrder:
    """An order resting on the book, as seen from outside."""

    order_id: OrderId
    trader: TraderId
    side: Side
    price: Price
    qty: Qty


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Every resting order, each side listed in priority order (best first).

    Two books with equal snapshots will behave identically on any future input,
    which is what makes snapshots useful for differential testing.
    """

    bids: tuple[RestingOrder, ...]
    asks: tuple[RestingOrder, ...]
