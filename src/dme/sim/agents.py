"""Simulated market participants.

Each agent is one trader with its own random stream. When it wakes it returns the
commands it wants to send, and it learns what happened to its orders only from the
events the engine publishes, the same way a real client would.

The mix is chosen to keep the engine busy in every code path:
  * market makers keep both sides of each book populated and requote often, so
    there is always resting liquidity and a steady stream of cancels;
  * random traders send limit and IOC orders near the fair price with random
    self-trade settings, cancel their own orders, occasionally probe IDs they do
    not own, and occasionally send malformed orders;
  * takers send market and aggressive IOC orders that sweep several levels.
Few traders share each book, so self-trade prevention triggers often.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

from dme.core import (
    InvariantViolation,
    OrderId,
    OrderSpec,
    OrderType,
    Price,
    Qty,
    SelfTradePrevention,
    Side,
    Symbol,
    TraderId,
)
from dme.engine import CancelOrder, Command, Sequenced, SubmitOrder
from dme.events import Accepted, Cancelled, Rejected, Rested, Trade

MIN_PRICE = 10


class Market:
    """Shared, slowly moving fair value per symbol that agents quote around."""

    def __init__(self, symbols: tuple[Symbol, ...], initial_price: Price, rng: random.Random):
        self.symbols = symbols
        self.fair: dict[Symbol, Price] = dict.fromkeys(symbols, initial_price)
        self.orders_issued = 0
        self._rng = rng

    def step(self) -> None:
        for symbol in self.symbols:
            move = self._rng.choice((-1, 0, 0, 1))
            self.fair[symbol] = max(MIN_PRICE, self.fair[symbol] + move)

    def observe(self, published: list[Sequenced]) -> None:
        if published and isinstance(published[0].event, Accepted | Rejected):
            self.orders_issued = max(self.orders_issued, published[0].event.order_id)


@dataclass
class LiveOrder:
    symbol: Symbol
    side: Side
    price: Price
    qty: Qty


class Agent(ABC):
    def __init__(self, trader: TraderId, rng: random.Random, market: Market) -> None:
        self.trader = trader
        self.rng = rng
        self.market = market
        self.live: dict[OrderId, LiveOrder] = {}

    @abstractmethod
    def act(self) -> list[Command]:
        """Commands to send now."""

    @abstractmethod
    def next_delay(self) -> int:
        """Simulated microseconds until this agent next wakes."""

    def observe(self, published: list[Sequenced]) -> None:
        """Track this agent's resting orders from the engine's events."""
        for s in published:
            event = s.event
            if isinstance(event, Rested) and event.trader == self.trader:
                if s.symbol is None:
                    raise InvariantViolation(f"Rested event without a symbol: {s}")
                self.live[event.order_id] = LiveOrder(s.symbol, event.side, event.price, event.qty)
            elif isinstance(event, Trade) and event.maker_id in self.live:
                order = self.live[event.maker_id]
                order.qty -= event.qty
                if order.qty == 0:
                    del self.live[event.maker_id]
            elif isinstance(event, Cancelled):
                self.live.pop(event.order_id, None)

    def _cancel(self, order_id: OrderId) -> CancelOrder:
        return CancelOrder(self.trader, order_id)

    def _random_side(self) -> Side:
        return self.rng.choice((Side.BUY, Side.SELL))


class MarketMaker(Agent):
    """Pulls its quotes in one symbol and requotes several levels on both sides
    around fair value."""

    LEVELS = 3

    def act(self) -> list[Command]:
        symbol = self.rng.choice(self.market.symbols)
        commands: list[Command] = [
            self._cancel(order_id) for order_id, o in self.live.items() if o.symbol == symbol
        ]
        fair = self.market.fair[symbol]
        half_spread = self.rng.randint(1, 3)
        for level in range(self.LEVELS):
            offset = half_spread + 2 * level
            for side, price in ((Side.BUY, fair - offset), (Side.SELL, fair + offset)):
                qty = self.rng.randint(5, 20)
                spec = OrderSpec.limit(self.trader, side, max(1, price), qty)
                commands.append(SubmitOrder(symbol, spec))
        return commands

    def next_delay(self) -> int:
        return self.rng.randint(200, 1_000)


class RandomTrader(Agent):
    def act(self) -> list[Command]:
        roll = self.rng.random()
        if roll < 0.15 and self.live:
            return [self._cancel(self.rng.choice(sorted(self.live)))]
        if roll < 0.20:
            # Probe an ID that is probably someone else's, or does not exist yet.
            upper = self.market.orders_issued + 5
            return [self._cancel(OrderId(self.rng.randint(1, upper)))]
        symbol = self.rng.choice(self.market.symbols)
        if roll < 0.22:
            return [SubmitOrder(symbol, self._malformed())]
        side = self._random_side()
        # Mostly passive orders that build depth; some that cross the spread.
        distance = self.rng.randint(0, 10) if self.rng.random() < 0.8 else -self.rng.randint(1, 4)
        fair = self.market.fair[symbol]
        price = max(1, fair - distance if side is Side.BUY else fair + distance)
        order_type = OrderType.LIMIT if self.rng.random() < 0.8 else OrderType.IOC
        stp = self.rng.choice(list(SelfTradePrevention))
        spec = OrderSpec(self.trader, side, order_type, price, self.rng.randint(1, 10), stp)
        return [SubmitOrder(symbol, spec)]

    def _malformed(self) -> OrderSpec:
        side = self._random_side()
        return self.rng.choice(
            [
                OrderSpec(self.trader, side, OrderType.LIMIT, 0, 5),
                OrderSpec(self.trader, side, OrderType.LIMIT, None, 5),
                OrderSpec(self.trader, side, OrderType.MARKET, 100, 5),
                OrderSpec(self.trader, side, OrderType.IOC, 100, 0),
            ]
        )

    def next_delay(self) -> int:
        return self.rng.randint(50, 2_000)


class Taker(Agent):
    """Sends aggressive orders that sweep the book."""

    def act(self) -> list[Command]:
        symbol = self.rng.choice(self.market.symbols)
        side = self._random_side()
        qty = self.rng.randint(1, 30)
        stp = self.rng.choice(list(SelfTradePrevention))
        if self.rng.random() < 0.5:
            spec = OrderSpec.market(self.trader, side, qty, stp)
        else:
            fair = self.market.fair[symbol]
            reach = self.rng.randint(0, 4)
            price = fair + reach if side is Side.BUY else max(1, fair - reach)
            spec = OrderSpec.ioc(self.trader, side, price, qty, stp)
        return [SubmitOrder(symbol, spec)]

    def next_delay(self) -> int:
        return self.rng.randint(200, 3_000)
