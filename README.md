# deterministic-matching-engine

A price-time priority matching engine whose correctness is checked by a deterministic
simulation harness: every run is a pure function of a seed, so any failure found among
millions of simulated orders replays exactly.

> Work in progress. Done: the single-instrument order book and its test oracles.
> Next: a multi-instrument engine, then the simulation harness with injected faults.

## Matching rules

- **Limit orders** match against the opposite side while prices cross, then any
  remainder rests on the book.
- **Price-time priority**: better price first; at the same price, earlier orders first.
- **Trades execute at the resting (maker) order's price**, so an aggressive order gets
  any price improvement.
- **Prices and quantities are integers** (ticks and lots). No floats, so results are
  exact and reproducible.
- Every command returns the full list of events it caused (`Accepted`, `Trade`,
  `Rested`, `Rejected`, `Cancelled`, `CancelRejected`).

## How correctness is checked

| Oracle | What it catches |
| --- | --- |
| `ReferenceBook` | A deliberately naive matcher that scans a flat list. The real book must emit identical events and snapshots for every input. |
| `Audit` | Rules on the event stream that hold for any correct book: quantity is conserved, trades only hit resting orders at their price and within the taker's limit, cancels remove exactly what rested. |
| `OrderBook.check_invariants` | Internal consistency: sorted levels, linked lists, cached totals, ID index, book never crossed. |

These run after every command in a seeded random test (200 seeds × 300 commands)
and a Hypothesis stateful test that shrinks any failure to a minimal command sequence.
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
