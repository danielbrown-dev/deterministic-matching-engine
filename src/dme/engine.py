"""The matching engine: one order book per symbol behind a single entry point.

What the engine adds on top of the books:
  * **Order IDs.** The engine assigns them from one counter, so IDs are unique
    across all symbols and are never reused. A cancel can never hit the wrong order.
  * **Sequence numbers.** Every event gets the next number from one global counter,
    so the full output of the engine is one totally ordered stream. Replaying the
    same commands always yields the same stream, which is what M3's determinism
    check and M4's recovery rely on.
  * **Routing and ownership.** A cancel names only an order ID; the engine finds the
    book, and only the trader who owns an order may cancel it.

The engine is single-threaded and does no I/O: like the books, its output is a pure
function of its input.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from dme.book import OrderBook
from dme.core import BookSnapshot, OrderId, OrderSpec, Symbol, TraderId
from dme.events import (
    Cancelled,
    CancelRejected,
    CancelRejectReason,
    Event,
    Rejected,
    RejectReason,
    Rested,
    Trade,
)


class Book(Protocol):
    """What the engine needs from an order book. ``OrderBook`` and the test-only
    ``ReferenceBook`` both satisfy it, so the same engine can drive either."""

    def submit(self, order_id: OrderId, spec: OrderSpec) -> list[Event]: ...
    def cancel(self, order_id: OrderId) -> list[Event]: ...
    def snapshot(self) -> BookSnapshot: ...
    def __contains__(self, order_id: object) -> bool: ...


@dataclass(frozen=True, slots=True)
class Sequenced:
    """An event as published by the engine."""

    seq: int
    symbol: Symbol | None
    """None only for a cancel rejected because the order could not be found."""
    event: Event


@dataclass(frozen=True, slots=True)
class _Owner:
    symbol: Symbol
    trader: TraderId


class Engine:
    def __init__(
        self, symbols: Iterable[Symbol], book_factory: Callable[[], Book] = OrderBook
    ) -> None:
        self._books: dict[Symbol, Book] = {symbol: book_factory() for symbol in symbols}
        self._next_order_id = 1
        self._next_seq = 1
        # Resting orders only: where each one lives and who owns it.
        self._owners: dict[OrderId, _Owner] = {}

    @property
    def symbols(self) -> tuple[Symbol, ...]:
        return tuple(self._books)

    def submit(self, symbol: Symbol, spec: OrderSpec) -> list[Sequenced]:
        """Assign an order ID and submit the order to the symbol's book.

        The assigned ID is in the first event (``Accepted`` or ``Rejected``).
        """
        order_id = OrderId(self._next_order_id)
        self._next_order_id += 1
        book = self._books.get(symbol)
        if book is None:
            return self._publish(symbol, [Rejected(order_id, RejectReason.UNKNOWN_SYMBOL)])
        events = book.submit(order_id, spec)
        self._track(symbol, book, events)
        return self._publish(symbol, events)

    def cancel(self, trader: TraderId, order_id: OrderId) -> list[Sequenced]:
        owner = self._owners.get(order_id)
        if owner is None or owner.trader != trader:
            rejected = CancelRejected(order_id, CancelRejectReason.UNKNOWN_ORDER)
            return self._publish(None, [rejected])
        book = self._books[owner.symbol]
        events = book.cancel(order_id)
        self._track(owner.symbol, book, events)
        return self._publish(owner.symbol, events)

    def snapshot(self, symbol: Symbol) -> BookSnapshot:
        return self._books[symbol].snapshot()

    def resting_owner(self, order_id: OrderId) -> tuple[Symbol, TraderId] | None:
        owner = self._owners.get(order_id)
        return None if owner is None else (owner.symbol, owner.trader)

    def _track(self, symbol: Symbol, book: Book, events: list[Event]) -> None:
        """Keep the owner index in step with what is resting."""
        for event in events:
            if isinstance(event, Rested):
                self._owners[event.order_id] = _Owner(symbol, event.trader)
            elif isinstance(event, Cancelled):
                del self._owners[event.order_id]
            elif isinstance(event, Trade) and event.maker_id not in book:
                del self._owners[event.maker_id]

    def _publish(self, symbol: Symbol | None, events: list[Event]) -> list[Sequenced]:
        start = self._next_seq
        self._next_seq += len(events)
        return [Sequenced(start + i, symbol, event) for i, event in enumerate(events)]
