"""A single-instrument limit order book with price-time priority.

Data structures
---------------
Each side is a set of price levels. Each level holds its resting orders in an
intrusive doubly linked list (oldest first), so appending at the tail, removing
the head during matching, and removing any order on cancel are all O(1).

Level prices are kept in a sorted list so the best level is always at the end:
bids are keyed by ``price`` and asks by ``-price``. Finding the best level is
O(1) and removing it is an O(1) ``pop``. Adding a new price level, or removing a
level that is not the best, costs a binary search plus a list shift, which is
O(L) in the number of levels on that side. Real books have few active levels
near the touch, and most new levels are created near it, so this is cheap in
practice; it is the one trade-off to revisit if benchmarks say otherwise.

A dict from order ID to its list node gives O(1) cancel lookup.

Complexity, with L price levels on a side and F fills produced:
  * submit: O(F) to match, where F counts fills and self-trade cancels;
    plus O(L) if it creates a new level
  * cancel: O(1); plus O(L) if it empties a level that is not the best
  * best bid / best ask: O(1)

Self-trade prevention
---------------------
When the incoming order reaches a resting order from the same trader, the incoming
order's ``SelfTradePrevention`` setting decides which side is cancelled. Orders
ahead of the conflict at better prices, or earlier at the same price, still trade.

Determinism
-----------
The book has no clock, no randomness and no I/O. Its output is a pure function
of the sequence of commands applied to it.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterator

from dme.core import (
    BookSnapshot,
    InvariantViolation,
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

__all__ = ["InvariantViolation", "OrderBook"]


class _Order:
    """A resting order: a node in its price level's linked list."""

    __slots__ = ("level", "next", "order_id", "prev", "qty", "side", "trader")

    def __init__(
        self, order_id: OrderId, trader: TraderId, side: Side, qty: Qty, level: _Level
    ) -> None:
        self.order_id = order_id
        self.trader = trader
        self.side = side
        self.qty = qty
        self.level = level
        self.prev: _Order | None = None
        self.next: _Order | None = None


class _Level:
    """All resting orders at one price on one side, oldest first."""

    __slots__ = ("count", "head", "price", "tail", "total_qty")

    def __init__(self, price: Price) -> None:
        self.price = price
        self.head: _Order | None = None
        self.tail: _Order | None = None
        self.total_qty: Qty = 0
        self.count = 0

    def append(self, order: _Order) -> None:
        order.prev = self.tail
        order.next = None
        if self.tail is None:
            self.head = order
        else:
            self.tail.next = order
        self.tail = order
        self.total_qty += order.qty
        self.count += 1

    def unlink(self, order: _Order) -> None:
        """Remove ``order`` from the list. Its remaining qty leaves the level total."""
        if order.prev is None:
            self.head = order.next
        else:
            order.prev.next = order.next
        if order.next is None:
            self.tail = order.prev
        else:
            order.next.prev = order.prev
        order.prev = order.next = None
        self.total_qty -= order.qty
        self.count -= 1

    def __iter__(self) -> Iterator[_Order]:
        node = self.head
        while node is not None:
            yield node
            node = node.next


class _HalfBook:
    """One side of the book: price levels sorted so the best one is last."""

    __slots__ = ("_keys", "_levels", "_sign", "side")

    def __init__(self, side: Side) -> None:
        self.side = side
        # Bids: higher price is better, so key = price. Asks: lower is better, key = -price.
        # Either way, the best level has the largest key and sits at the end of _keys.
        self._sign = 1 if side is Side.BUY else -1
        self._keys: list[int] = []
        self._levels: dict[int, _Level] = {}

    def best(self) -> _Level | None:
        if not self._keys:
            return None
        return self._levels[self._keys[-1]]

    def level_at(self, price: Price) -> _Level:
        """The level at ``price``, created if it does not exist."""
        key = self._sign * price
        level = self._levels.get(key)
        if level is None:
            level = _Level(price)
            self._levels[key] = level
            bisect.insort(self._keys, key)
        return level

    def remove_level(self, level: _Level) -> None:
        key = self._sign * level.price
        if self._keys and self._keys[-1] == key:
            self._keys.pop()  # the common case: the best level was exhausted
        else:
            index = bisect.bisect_left(self._keys, key)
            del self._keys[index]
        del self._levels[key]

    def levels(self) -> Iterator[_Level]:
        """Levels from best to worst."""
        for key in reversed(self._keys):
            yield self._levels[key]

    def check_invariants(self) -> int:
        """Check this side's structure. Returns the number of resting orders."""
        if any(a >= b for a, b in zip(self._keys, self._keys[1:], strict=False)):
            raise InvariantViolation(f"{self.side.name} keys not strictly increasing")
        if set(self._keys) != set(self._levels):
            raise InvariantViolation(f"{self.side.name} keys and levels disagree")
        orders = 0
        for key, level in self._levels.items():
            if key != self._sign * level.price:
                raise InvariantViolation(f"level {level.price} stored under wrong key {key}")
            if level.head is None:
                raise InvariantViolation(f"empty level {level.price} left on the book")
            if level.head.prev is not None or level.tail is None or level.tail.next is not None:
                raise InvariantViolation(f"level {level.price} list ends are inconsistent")
            total = count = 0
            prev: _Order | None = None
            for order in level:
                if order.prev is not prev:
                    raise InvariantViolation(f"order {order.order_id} has a broken back link")
                if order.qty <= 0:
                    raise InvariantViolation(f"order {order.order_id} rests with qty {order.qty}")
                if order.level is not level or order.side is not self.side:
                    raise InvariantViolation(f"order {order.order_id} is on the wrong level")
                total += order.qty
                count += 1
                prev = order
            if prev is not level.tail:
                raise InvariantViolation(f"level {level.price} tail is not its last order")
            if total != level.total_qty or count != level.count:
                raise InvariantViolation(f"level {level.price} cached totals are stale")
            orders += count
        return orders


class OrderBook:
    """A limit order book for one instrument.

    Order IDs are chosen by the caller and must be unique among resting orders.
    Making IDs globally unique, so a cancel can never hit a reused ID, is the
    engine's job. So is checking who may cancel an order: the book cancels any
    resting order it is asked to.
    """

    __slots__ = ("_asks", "_bids", "_orders")

    def __init__(self) -> None:
        self._bids = _HalfBook(Side.BUY)
        self._asks = _HalfBook(Side.SELL)
        self._orders: dict[OrderId, _Order] = {}

    # Commands

    def submit(self, order_id: OrderId, spec: OrderSpec) -> list[Event]:
        """Submit an order: match it against the book, then rest or expire any remainder.

        Validation checks run in this order and the first failure is reported:
        price, quantity, duplicate ID.
        """
        if not spec.has_valid_price():
            return [Rejected(order_id, RejectReason.INVALID_PRICE)]
        if spec.qty <= 0:
            return [Rejected(order_id, RejectReason.NON_POSITIVE_QTY)]
        if order_id in self._orders:
            return [Rejected(order_id, RejectReason.DUPLICATE_ORDER_ID)]

        events: list[Event] = [Accepted(order_id, spec)]
        remaining, stopped_by_stp = self._match(order_id, spec, events)
        if remaining == 0:
            pass
        elif stopped_by_stp:
            events.append(Expired(order_id, remaining, ExpireReason.SELF_TRADE))
        elif spec.order_type is OrderType.LIMIT:
            assert spec.price is not None  # guaranteed by has_valid_price
            level = self._half(spec.side).level_at(spec.price)
            order = _Order(order_id, spec.trader, spec.side, remaining, level)
            level.append(order)
            self._orders[order_id] = order
            events.append(Rested(order_id, spec.trader, spec.side, spec.price, remaining))
        else:
            events.append(Expired(order_id, remaining, ExpireReason.UNFILLED))
        return events

    def cancel(self, order_id: OrderId) -> list[Event]:
        """Remove a resting order from the book."""
        order = self._orders.get(order_id)
        if order is None:
            return [CancelRejected(order_id, CancelRejectReason.UNKNOWN_ORDER)]
        level = order.level
        self._remove(order)
        if level.head is None:
            self._half(order.side).remove_level(level)
        return [Cancelled(order_id, order.qty, CancelReason.REQUESTED)]

    # Queries

    def best_bid(self) -> Price | None:
        level = self._bids.best()
        return None if level is None else level.price

    def best_ask(self) -> Price | None:
        level = self._asks.best()
        return None if level is None else level.price

    def depth(self, side: Side) -> list[tuple[Price, Qty]]:
        """Aggregate (price, total quantity) per level, best first."""
        return [(level.price, level.total_qty) for level in self._half(side).levels()]

    def snapshot(self) -> BookSnapshot:
        return BookSnapshot(bids=self._resting(self._bids), asks=self._resting(self._asks))

    def __len__(self) -> int:
        """Number of resting orders."""
        return len(self._orders)

    def __contains__(self, order_id: object) -> bool:
        return order_id in self._orders

    def check_invariants(self) -> None:
        """Verify every structural invariant. Raises ``InvariantViolation`` on failure.

        This walks the whole book, so it is for tests and the simulator, not the hot
        path. It raises rather than using ``assert`` so it still runs under ``python -O``.
        """
        resting = self._bids.check_invariants() + self._asks.check_invariants()
        if resting != len(self._orders):
            raise InvariantViolation(
                f"{resting} orders on levels but {len(self._orders)} in the ID index"
            )
        for order_id, order in self._orders.items():
            if order.order_id != order_id:
                raise InvariantViolation(f"index entry {order_id} points at {order.order_id}")
        bid, ask = self.best_bid(), self.best_ask()
        if bid is not None and ask is not None and bid >= ask:
            raise InvariantViolation(f"book is crossed: best bid {bid} >= best ask {ask}")

    # Internals

    def _half(self, side: Side) -> _HalfBook:
        return self._bids if side is Side.BUY else self._asks

    def _remove(self, order: _Order) -> None:
        """Take a resting order off its level and out of the index. Leaves the level,
        even if now empty, for the caller to remove."""
        order.level.unlink(order)
        del self._orders[order.order_id]

    def _match(self, taker_id: OrderId, spec: OrderSpec, events: list[Event]) -> tuple[Qty, bool]:
        """Fill the taker against the opposite side.

        Returns the unfilled quantity, and whether self-trade prevention stopped the
        taker (in which case the remainder must expire rather than rest).
        """
        opposite = self._half(spec.side.opposite)
        remaining = spec.qty
        stopped = False
        while remaining > 0 and not stopped:
            level = opposite.best()
            if level is None or not spec.accepts(level.price):
                break
            while remaining > 0 and (maker := level.head) is not None:
                if maker.trader == spec.trader:
                    if spec.stp is not SelfTradePrevention.CANCEL_INCOMING:
                        self._remove(maker)
                        events.append(Cancelled(maker.order_id, maker.qty, CancelReason.SELF_TRADE))
                    if spec.stp is not SelfTradePrevention.CANCEL_RESTING:
                        stopped = True
                        break
                    continue
                fill = min(remaining, maker.qty)
                events.append(Trade(maker.order_id, taker_id, level.price, fill))
                remaining -= fill
                if fill == maker.qty:
                    self._remove(maker)
                else:
                    maker.qty -= fill
                    level.total_qty -= fill
            if level.head is None:
                opposite.remove_level(level)
        return remaining, stopped

    @staticmethod
    def _resting(half: _HalfBook) -> tuple[RestingOrder, ...]:
        return tuple(
            RestingOrder(order.order_id, order.trader, order.side, level.price, order.qty)
            for level in half.levels()
            for order in level
        )
