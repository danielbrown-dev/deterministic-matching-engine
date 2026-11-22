"""Seeded random differential tests: the real implementation must match the
reference model exactly.

This is a small preview of the M3 simulator. Each run is a pure function of its
seed, so a failure message names the seed and step that reproduce it exactly.
"""

from __future__ import annotations

import random

import pytest
from harness import BookCommand, BookPair, Cancel, EnginePair, Submit, random_spec

from dme.core import OrderId, Symbol, TraderId

SEEDS = range(200)
STEPS = 300
SYMBOLS = [Symbol("AAPL"), Symbol("MSFT")]


def random_book_command(rng: random.Random, next_id: int) -> BookCommand:
    if rng.random() < 0.3:
        # Mostly IDs that exist or did exist, sometimes ones that never will.
        return Cancel(OrderId(rng.randrange(next_id + 3)))
    reuse = next_id > 0 and rng.random() < 0.05
    order_id = rng.randrange(next_id) if reuse else next_id
    return Submit(OrderId(order_id), random_spec(rng))


@pytest.mark.parametrize("seed", SEEDS)
def test_book_matches_reference(seed: int) -> None:
    rng = random.Random(seed)
    pair = BookPair()
    next_id = 0
    for step in range(STEPS):
        command = random_book_command(rng, next_id)
        if isinstance(command, Submit) and command.order_id == next_id:
            next_id += 1
        try:
            pair.apply(command)
        except Exception as exc:
            raise AssertionError(f"seed={seed} step={step}: {exc}") from exc


@pytest.mark.parametrize("seed", SEEDS)
def test_engine_matches_reference(seed: int) -> None:
    rng = random.Random(seed)
    pair = EnginePair(SYMBOLS)
    issued = 0
    for step in range(STEPS):
        try:
            if rng.random() < 0.3:
                resting = list(pair.audit.resting().values())
                if resting and rng.random() < 0.5:
                    # A resting order, usually cancelled by its owner.
                    order = rng.choice(resting)
                    owner = rng.random() < 0.8
                    trader = order.trader if owner else TraderId(rng.randrange(3))
                    pair.cancel(trader, order.order_id)
                else:
                    # Any trader, any ID: probes ownership as well as existence.
                    order_id = OrderId(rng.randrange(1, issued + 3))
                    pair.cancel(TraderId(rng.randrange(3)), order_id)
            else:
                symbol = (
                    rng.choice([*SYMBOLS, Symbol("UNKNOWN")])
                    if rng.random() < 0.02
                    else rng.choice(SYMBOLS)
                )
                pair.submit(symbol, random_spec(rng))
                issued += 1
        except Exception as exc:
            raise AssertionError(f"seed={seed} step={step}: {exc}") from exc
