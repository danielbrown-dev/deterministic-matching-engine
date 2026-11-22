"""Seeded random differential test: the book must match the reference model.

This is a small preview of the M3 simulator. Each run is a pure function of its
seed, so a failure message names the seed and step that reproduce it exactly.
Prices are drawn from a narrow band so orders cross often.
"""

from __future__ import annotations

import random

import pytest
from harness import Cancel, Command, Pair, Submit

from dme.core import OrderId, Side

SEEDS = range(200)
STEPS = 300


def random_command(rng: random.Random, next_id: int) -> Command:
    if rng.random() < 0.3:
        # Mostly IDs that exist or did exist, sometimes ones that never will.
        return Cancel(OrderId(rng.randrange(next_id + 3)))
    order_id = next_id if rng.random() < 0.95 else rng.randrange(max(next_id, 1))
    price = rng.randint(95, 105) if rng.random() < 0.98 else rng.choice([0, -1])
    qty = rng.randint(1, 10) if rng.random() < 0.98 else rng.choice([0, -3])
    return Submit(OrderId(order_id), rng.choice([Side.BUY, Side.SELL]), price, qty)


@pytest.mark.parametrize("seed", SEEDS)
def test_book_matches_reference(seed: int) -> None:
    rng = random.Random(seed)
    pair = Pair()
    next_id = 0
    for step in range(STEPS):
        command = random_command(rng, next_id)
        if isinstance(command, Submit) and command.order_id == next_id:
            next_id += 1
        try:
            pair.apply(command)
        except Exception as exc:
            raise AssertionError(f"seed={seed} step={step}: {exc}") from exc
