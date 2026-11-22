"""Snapshots and crash recovery."""

from __future__ import annotations

import random

import pytest
from harness import random_spec

from dme.book import OrderBook
from dme.core import OrderSpec, Side, Symbol, TraderId
from dme.engine import Engine
from dme.gateway import CancelRequest, ClientOrderId, Exchange, NewOrderRequest, Request
from dme.persistence import Storage, recover

SYMBOLS = [Symbol("AAPL"), Symbol("MSFT")]


def random_requests(seed: int, count: int) -> list[Request]:
    rng = random.Random(seed)
    requests: list[Request] = []
    next_id = {TraderId(t): 1 for t in range(3)}
    for _ in range(count):
        trader = TraderId(rng.randrange(3))
        cid = ClientOrderId(next_id[trader])
        next_id[trader] += 1
        if rng.random() < 0.3:
            target = ClientOrderId(rng.randrange(1, cid + 1))
            requests.append(CancelRequest(trader, cid, target))
        else:
            spec = random_spec(rng)
            spec = OrderSpec(trader, spec.side, spec.order_type, spec.price, spec.qty, spec.stp)
            requests.append(NewOrderRequest(trader, cid, rng.choice(SYMBOLS), spec))
    return requests


def test_book_snapshot_round_trips_with_priority() -> None:
    book = OrderBook()
    engine = Engine([SYMBOLS[0]], lambda: book)
    rng = random.Random(1)
    for _ in range(300):
        engine.submit(SYMBOLS[0], random_spec(rng))
    assert len(book) > 10  # a book worth restoring
    restored = OrderBook.from_snapshot(book.snapshot())
    assert restored.snapshot() == book.snapshot()


def test_engine_state_round_trips_and_behaves_identically() -> None:
    engine = Engine(SYMBOLS)
    rng = random.Random(2)
    for _ in range(300):
        engine.submit(rng.choice(SYMBOLS), random_spec(rng))
    restored = Engine.from_state(engine.state())
    assert restored.state() == engine.state()
    for _ in range(300):
        symbol, spec = rng.choice(SYMBOLS), random_spec(rng)
        assert restored.submit(symbol, spec) == engine.submit(symbol, spec)


@pytest.mark.parametrize("snapshot_at", [None, 0, 1, 150, 299, 300])
def test_recovery_reproduces_the_exchange_exactly(snapshot_at: int | None) -> None:
    requests = random_requests(3, 300)
    original = Exchange(Engine(SYMBOLS))
    storage = Storage()
    for i, request in enumerate(requests):
        if i == snapshot_at:
            storage.save_snapshot(original.state())
        storage.append(request)
        original.handle(request)
    if snapshot_at == len(requests):
        storage.save_snapshot(original.state())
    assert recover(storage, SYMBOLS).state() == original.state()


def test_crash_between_logging_and_applying_is_recovered() -> None:
    """The request is in the log but was never applied: recovery applies it."""
    requests = random_requests(4, 50)
    live = Exchange(Engine(SYMBOLS))
    storage = Storage()
    for request in requests[:-1]:
        storage.append(request)
        live.handle(request)
    storage.append(requests[-1])  # logged... then the process dies

    reference = Exchange(Engine(SYMBOLS))
    for request in requests:
        reference.handle(request)
    assert recover(storage, SYMBOLS).state() == reference.state()


def test_recovered_exchange_ignores_retry_of_logged_request() -> None:
    """After recovery, a client retrying a request that was logged before the crash
    must not enter it twice."""
    request = NewOrderRequest(
        TraderId(1), ClientOrderId(1), SYMBOLS[0], OrderSpec.limit(TraderId(1), Side.BUY, 100, 5)
    )
    storage = Storage()
    storage.append(request)
    recovered = recover(storage, SYMBOLS)
    assert recovered.handle(request) == []
    assert len(recovered.engine.snapshot(SYMBOLS[0]).bids) == 1
