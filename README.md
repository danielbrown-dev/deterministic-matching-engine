# deterministic-matching-engine

A price-time priority matching engine whose correctness is checked by a deterministic
simulation harness: every run is a pure function of a seed, so any failure found among
millions of simulated orders replays exactly.

> Work in progress. Done: the order book, the multi-symbol engine, the gateway, crash
> recovery, and a deterministic simulator that injects network faults and crashes.
> Next: benchmarks and polish.

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

## Gateway and crash recovery

Clients talk to the exchange over a network that can lose, duplicate, delay and reorder
messages, and the exchange can crash at any moment. Two mechanisms make that safe:

- **Exactly-once order entry.** Clients name each request with their own *client order
  ID* (as in FIX). The gateway ignores any request it has already handled, so clients
  can retry freely and no order is ever entered twice. Cancels name the client order ID
  too, so a client can cancel an order whose acknowledgment it never received.
- **Reliable reports.** Each report carries a per-trader sequence number. Clients
  process reports strictly in order, drop duplicates, and request a resend of anything
  missing, which also acknowledges what they have.

Durability uses a write-ahead log: each request is logged before it is applied, and
the exchange snapshots its state periodically. Because the exchange is deterministic,
recovery is just: load the latest snapshot and replay the log after it. A crash between
logging a request and applying it is safe: recovery applies it, and the client's retry
is recognized as a duplicate.

## Deterministic simulation

```sh
python -m dme.sim --seed 7 --steps 100000
python -m dme.sim --seeds 0:100 --steps 5000 --faults all
python -m dme.sim --seeds 0:20 --steps 5000 --reference
```

A simulation is a pure function of its seed. Market makers, random traders and takers
wake on a simulated clock (no wall-clock reads anywhere) and trade through client
sessions over the simulated network. They learn what happened only from the reports
they receive. Each component draws from its own seeded random stream.

`--faults` injects message loss (5%), duplication (5%) and reordering (latencies from
10 to 600 µs), exchange crashes, and crashes in the window between logging a request
and applying it. The simulator checks:

- **A twin exchange that never crashes** handles every request the moment it is logged,
  with every engine command checked against the invariants below. The live exchange
  must send exactly the twin's reports, and after every recovery its state must equal
  the twin's.
- **Exactly once:** no client order ID is ever accepted twice.
- **In-order delivery:** every report a client processes is exactly the next one in its
  stream.
- **Liveness:** no client request goes unanswered for more than 100 ms of simulated time.
- **Convergence:** at the end, faults stop and the network drains, and every client's
  view of its resting orders must match the exchange exactly.

If anything fails, the report names the seed and step and prints the exact command that
reproduces it, plus the last 20 requests. This is a real report, from a bug planted
in a test (the gateway forgetting which requests it has seen):

```
seed=0 failed at log position 2 (t=473us): InvariantViolation: client order (7, 1) accepted twice
reproduce with: python -m dme.sim --seed 0 --steps 3 --faults net
```

Every command and event feeds a SHA-256 digest of the run. The same seed must give the
same digest in any process; a test checks this across different `PYTHONHASHSEED`
values, which catches any accidental dependence on Python's salted string hashing.
CI simulates fixed seeds on every change, with and without faults; a nightly job runs
100 fresh seeds in each fault mode.

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
