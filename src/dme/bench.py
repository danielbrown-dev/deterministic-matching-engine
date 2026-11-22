"""Throughput and latency benchmarks.

    python -m dme.bench
    python -m dme.bench --ops 500000 --seed 3

Each benchmark replays a pre-generated, seeded workload, so runs are comparable:
generating the workload is not timed, only applying it. The mix approximates a busy
book: mostly passive limit orders near a drifting mid price, some aggressive limit,
IOC and market orders, and cancels of recent orders (some of them already gone).

Per-operation latency is measured with ``perf_counter_ns`` around each call, which
adds its own overhead (tens of nanoseconds), so percentiles are upper bounds.
Throughput is measured separately, without per-operation timing.

No checks run here: this measures the code that would run in production.
"""

from __future__ import annotations

import argparse
import gc
import platform
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from dme.book import OrderBook
from dme.core import OrderId, OrderSpec, OrderType, SelfTradePrevention, Side, Symbol, TraderId
from dme.engine import Engine
from dme.gateway import CancelRequest, ClientOrderId, Exchange, NewOrderRequest, Request
from dme.reference import ReferenceBook

SYMBOLS = (Symbol("AAPL"), Symbol("MSFT"), Symbol("GOOG"))
TRADERS = 20


@dataclass(frozen=True)
class Op:
    kind: str  # "submit" or "cancel"
    order_id: OrderId
    spec: OrderSpec | None
    symbol: Symbol


def workload(seed: int, ops: int) -> list[Op]:
    rng = random.Random(seed)
    mid = {symbol: 10_000 for symbol in SYMBOLS}
    issued: list[OrderId] = []
    result: list[Op] = []
    for n in range(1, ops + 1):
        symbol = rng.choice(SYMBOLS)
        mid[symbol] += rng.choice((-1, 0, 0, 1))
        roll = rng.random()
        if roll < 0.15 and issued:
            target = issued[-rng.randint(1, min(len(issued), 500))]
            result.append(Op("cancel", target, None, symbol))
            continue
        order_id = OrderId(n)
        issued.append(order_id)
        trader = TraderId(rng.randrange(TRADERS))
        side = rng.choice((Side.BUY, Side.SELL))
        qty = rng.randint(1, 20)
        stp = SelfTradePrevention.CANCEL_RESTING
        if roll < 0.75:  # passive limit
            distance = rng.randint(1, 20)
            price = mid[symbol] - distance if side is Side.BUY else mid[symbol] + distance
            spec = OrderSpec(trader, side, OrderType.LIMIT, price, qty, stp)
        elif roll < 0.90:  # aggressive limit
            reach = rng.randint(0, 5)
            price = mid[symbol] + reach if side is Side.BUY else mid[symbol] - reach
            spec = OrderSpec(trader, side, OrderType.LIMIT, price, qty, stp)
        elif roll < 0.96:
            reach = rng.randint(0, 5)
            price = mid[symbol] + reach if side is Side.BUY else mid[symbol] - reach
            spec = OrderSpec(trader, side, OrderType.IOC, price, qty, stp)
        else:
            spec = OrderSpec(trader, side, OrderType.MARKET, None, qty, stp)
        result.append(Op("submit", order_id, spec, symbol))
    return result


def book_runner(
    make: Callable[[], OrderBook | ReferenceBook],
) -> Callable[[list[Op]], Callable[[Op], object]]:
    """One book per symbol, driven directly with the workload's order IDs."""

    def setup(_: list[Op]) -> Callable[[Op], object]:
        books = {symbol: make() for symbol in SYMBOLS}

        def apply(op: Op) -> object:
            book = books[op.symbol]
            if op.spec is not None:
                return book.submit(op.order_id, op.spec)
            return book.cancel(op.order_id)

        return apply

    return setup


def engine_runner(_: list[Op]) -> Callable[[Op], object]:
    """The engine assigns its own IDs; it issues them in workload order, and cancels
    carry the owner, so the workload's IDs line up."""
    engine = Engine(SYMBOLS)
    owners: dict[OrderId, TraderId] = {}

    def apply(op: Op) -> object:
        if op.spec is not None:
            owners[op.order_id] = op.spec.trader
            return engine.submit(op.symbol, op.spec)
        return engine.cancel(owners[op.order_id], op.order_id)

    return apply


def exchange_runner(ops: list[Op]) -> Callable[[Op], object]:
    """Through the gateway: client order IDs, deduplication and report sequencing."""
    exchange = Exchange(Engine(SYMBOLS))
    requests: dict[int, Request] = {}
    next_id: dict[TraderId, int] = {}
    client_ids: dict[OrderId, tuple[TraderId, ClientOrderId]] = {}
    for op in ops:  # translate up front so translation is not timed
        if op.spec is not None:
            trader = op.spec.trader
            cid = ClientOrderId(next_id.get(trader, 0) + 1)
            next_id[trader] = cid
            client_ids[op.order_id] = (trader, cid)
            requests[id(op)] = NewOrderRequest(trader, cid, op.symbol, op.spec)
        else:
            trader, target = client_ids[op.order_id]
            cid = ClientOrderId(next_id[trader] + 1)
            next_id[trader] = cid
            requests[id(op)] = CancelRequest(trader, cid, target)

    def apply(op: Op) -> object:
        return exchange.handle(requests[id(op)])

    return apply


@dataclass(frozen=True)
class Result:
    name: str
    ops: int
    ops_per_sec: float
    p50_ns: int
    p99_ns: int
    p999_ns: int


def measure(
    name: str, setup: Callable[[list[Op]], Callable[[Op], object]], ops: list[Op]
) -> Result:
    gc.collect()
    apply = setup(ops)
    started = time.perf_counter()
    for op in ops:
        apply(op)
    elapsed = time.perf_counter() - started

    apply = setup(ops)
    latencies: list[int] = []
    clock = time.perf_counter_ns
    for op in ops:
        t0 = clock()
        apply(op)
        latencies.append(clock() - t0)
    latencies.sort()

    def pct(p: float) -> int:
        return latencies[min(len(latencies) - 1, int(p * len(latencies)))]

    return Result(name, len(ops), len(ops) / elapsed, pct(0.50), pct(0.99), pct(0.999))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dme.bench")
    parser.add_argument("--ops", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    ops = workload(args.seed, args.ops)
    reference_ops = ops[: max(1, args.ops // 10)]
    print(f"Python {platform.python_version()} on {platform.machine()}, {args.ops:,} operations")
    print(f"{'benchmark':<24}{'ops':>9}{'ops/sec':>12}{'p50':>9}{'p99':>9}{'p99.9':>9}")
    results = [
        measure("OrderBook", book_runner(OrderBook), ops),
        measure("Engine", engine_runner, ops),
        measure("Exchange (gateway)", exchange_runner, ops),
        measure("ReferenceBook (oracle)", book_runner(ReferenceBook), reference_ops),
    ]
    for r in results:
        print(
            f"{r.name:<24}{r.ops:>9,}{r.ops_per_sec:>12,.0f}"
            f"{r.p50_ns / 1000:>8.1f}µs{r.p99_ns / 1000:>7.1f}µs{r.p999_ns / 1000:>7.1f}µs"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
