"""Core value types shared by the order book, the reference model and the simulator.

Prices and quantities are integers: prices in ticks, quantities in lots. Floats are
never used, so arithmetic is exact and results are reproducible across platforms.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import NewType

OrderId = NewType("OrderId", int)
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


@dataclass(frozen=True, slots=True)
class RestingOrder:
    """An order resting on the book, as seen from outside."""

    order_id: OrderId
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
