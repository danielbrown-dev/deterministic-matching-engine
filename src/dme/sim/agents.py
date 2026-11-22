"""Simulated market participants.

Each agent is one trader with its own random stream and its own client session.
When it wakes it places and cancels orders through the session, naming its orders
by client order ID, and it learns what happened only from the reports the session
delivers, the same way a real client would.

The mix is chosen to keep the exchange busy in every code path:
  * market makers keep several levels on both sides of each book and requote often,
    so there is always resting liquidity and a steady stream of cancels;
  * random traders send mostly passive limit orders that build depth, some
    aggressive limit and IOC orders, random self-trade settings, cancels of their
    own orders, cancels of IDs that are not live orders, and malformed orders;
  * takers send market and aggressive IOC orders that sweep several levels.
Few traders share each book, so self-trade prevention triggers often.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

from dme.core import OrderSpec, OrderType, Price, Qty, SelfTradePrevention, Side, Symbol, TraderId
from dme.events import Cancelled, CancelRejected, Rested, Trade
from dme.gateway import ClientOrderId, Report
from dme.sim.session import ClientSession

MIN_PRICE = 10


class Market:
    """Shared, slowly moving fair value per symbol that agents quote around."""

    def __init__(self, symbols: tuple[Symbol, ...], initial_price: Price, rng: random.Random):
        self.symbols = symbols
        self.fair: dict[Symbol, Price] = dict.fromkeys(symbols, initial_price)
        self._rng = rng

    def step(self) -> None:
        for symbol in self.symbols:
            move = self._rng.choice((-1, 0, 0, 1))
            self.fair[symbol] = max(MIN_PRICE, self.fair[symbol] + move)


@dataclass
class LiveOrder:
    symbol: Symbol
    side: Side
    price: Price
    qty: Qty


class Agent(ABC):
    def __init__(
        self, trader: TraderId, rng: random.Random, market: Market, session: ClientSession
    ) -> None:
        self.trader = trader
        self.rng = rng
        self.market = market
        self.session = session
        self.live: dict[ClientOrderId, LiveOrder] = {}
        """This agent's resting orders, as it knows them from reports."""
        self._symbols: dict[ClientOrderId, Symbol] = {}
        self._cancelling: set[ClientOrderId] = set()

    @abstractmethod
    def act(self) -> None:
        """Place and cancel orders through the session."""

    @abstractmethod
    def next_delay(self) -> int:
        """Simulated microseconds until this agent next wakes."""

    def observe(self, report: Report) -> None:
        """Track this agent's resting orders from reports, delivered exactly once and
        in order by the session."""
        cid, event = report.client_order_id, report.event
        if isinstance(event, Rested):
            self.live[cid] = LiveOrder(self._symbols[cid], event.side, event.price, event.qty)
        elif isinstance(event, Trade) and cid in self.live:
            order = self.live[cid]
            order.qty -= event.qty
            if order.qty == 0:
                del self.live[cid]
                self._cancelling.discard(cid)
        elif isinstance(event, Cancelled):
            self.live.pop(cid, None)
            self._cancelling.discard(cid)
        elif isinstance(event, CancelRejected):
            self._cancelling.discard(cid)

    def _place(self, symbol: Symbol, spec: OrderSpec) -> None:
        cid = self.session.place(symbol, spec)
        self._symbols[cid] = symbol

    def _cancel(self, cid: ClientOrderId) -> None:
        if cid not in self._cancelling:
            self._cancelling.add(cid)
            self.session.cancel(cid)

    def _random_side(self) -> Side:
        return self.rng.choice((Side.BUY, Side.SELL))


class MarketMaker(Agent):
    """Pulls its quotes in one symbol and requotes several levels on both sides
    around fair value."""

    LEVELS = 3

    def act(self) -> None:
        symbol = self.rng.choice(self.market.symbols)
        for cid in sorted(c for c, o in self.live.items() if o.symbol == symbol):
            self._cancel(cid)
        fair = self.market.fair[symbol]
        half_spread = self.rng.randint(1, 3)
        for level in range(self.LEVELS):
            offset = half_spread + 2 * level
            for side, price in ((Side.BUY, fair - offset), (Side.SELL, fair + offset)):
                qty = self.rng.randint(5, 20)
                self._place(symbol, OrderSpec.limit(self.trader, side, max(1, price), qty))

    def next_delay(self) -> int:
        return self.rng.randint(200, 1_000)


class RandomTrader(Agent):
    def act(self) -> None:
        roll = self.rng.random()
        if roll < 0.15 and self.live:
            self._cancel(self.rng.choice(sorted(self.live)))
            return
        if roll < 0.20:
            # An ID that is not a live order: filled, cancelled, a cancel request's
            # own ID, or not issued yet.
            target = self.rng.randint(1, self.session.last_issued_id + 3)
            self.session.cancel(ClientOrderId(target))
            return
        symbol = self.rng.choice(self.market.symbols)
        if roll < 0.22:
            self._place(symbol, self._malformed())
            return
        side = self._random_side()
        # Mostly passive orders that build depth; some that cross the spread.
        distance = self.rng.randint(0, 10) if self.rng.random() < 0.8 else -self.rng.randint(1, 4)
        fair = self.market.fair[symbol]
        price = max(1, fair - distance if side is Side.BUY else fair + distance)
        order_type = OrderType.LIMIT if self.rng.random() < 0.8 else OrderType.IOC
        stp = self.rng.choice(list(SelfTradePrevention))
        spec = OrderSpec(self.trader, side, order_type, price, self.rng.randint(1, 10), stp)
        self._place(symbol, spec)

    def _malformed(self) -> OrderSpec:
        side = self._random_side()
        return self.rng.choice(
            [
                OrderSpec(self.trader, side, OrderType.LIMIT, 0, 5),
                OrderSpec(self.trader, side, OrderType.LIMIT, None, 5),
                OrderSpec(self.trader, side, OrderType.MARKET, 100, 5),
                OrderSpec(self.trader, side, OrderType.IOC, 100, 0),
                # Claims to be another trader: the gateway must refuse it.
                OrderSpec(TraderId(self.trader + 1000), side, OrderType.LIMIT, 100, 5),
            ]
        )

    def next_delay(self) -> int:
        return self.rng.randint(50, 2_000)


class Taker(Agent):
    """Sends aggressive orders that sweep the book."""

    def act(self) -> None:
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
        self._place(symbol, spec)

    def next_delay(self) -> int:
        return self.rng.randint(200, 3_000)
