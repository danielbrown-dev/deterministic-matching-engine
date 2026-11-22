# deterministic-matching-engine

A price-time priority matching engine whose correctness is checked by a deterministic
simulation harness: every run is a pure function of a seed, so any failure found among
millions of simulated orders replays exactly.

> Work in progress. Done: the order book, the multi-symbol engine, and their test
> oracles. Next: the simulation harness, then injected network faults and crashes.

## Matching rules

- **Order types**: *limit* orders rest any unfilled remainder on the book;
  *immediate-or-cancel (IOC)* orders match up to their limit and expire the rest;
  *market* orders match at any price and expire the rest.
- **Price-time priority**: better price first; at the same price, earlier orders first.
- **Trades execute at the resting (maker) order's price**, so an aggressive order gets
  any price improvement.
- **Prices and quantities are integers** (ticks and lots). No floats, so results are
  exact and reproducible.
- **Self-trade prevention**: an order never trades with a resting order from the same
  trader. The incoming order chooses what happens: cancel the resting order and keep
  matching (the default, as on CME Globex), expire the incoming order, or both.
- Every command returns the full list of events it caused (`Accepted`, `Trade`,
  `Rested`, `Expired`, `Rejected`, `Cancelled`, `CancelRejected`).

## Engine

`Engine` runs one book per symbol behind a single entry point. It assigns order IDs
from one counter, so they are unique across symbols and never reused, and stamps every
event with a global sequence number, so its whole output is one totally ordered stream.
Only an order's owner can cancel it, and a rejected cancel looks the same whether the
order never existed or belongs to someone else, so cancels cannot probe other traders'
orders.

## How correctness is checked

| Oracle | What it catches |
| --- | --- |
| `ReferenceBook` | A deliberately naive matcher that scans a flat list. The real book must emit identical events and snapshots for every input. |
| `Audit` | Rules on the event stream that hold for any correct book: quantity is conserved, trades only hit resting orders at their price and within the taker's limit, no trader ever trades with itself, self-trade prevention cancels exactly what the mode says, only limit orders rest, only owners can cancel. |
| `OrderBook.check_invariants` | Internal consistency: sorted levels, linked lists, cached totals, ID index, book never crossed. |

These run after every command in seeded random tests (200 seeds × 300 commands, for
the book and for the engine) and Hypothesis stateful tests that shrink any failure to a
minimal command sequence. The engine is also checked for contiguous sequence numbers
and an owner index that matches the books exactly.
`tests/test_checkers.py` corrupts state on purpose to prove each oracle actually fires.

## Data structures

Each price level is an intrusive doubly linked list of orders, so adding an order,
filling the front of the queue, and cancelling from the middle are all O(1). Levels
are kept in a sorted list with the best price at the end, so the best price is O(1)
to read and remove. A dict from order ID to list node gives O(1) cancel lookup.
Details and trade-offs are in the `dme/book.py` docstring.

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```
