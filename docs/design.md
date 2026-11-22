# Design notes

Why the system is built the way it is, what each choice costs, and what it does not do.

## Determinism is the foundation

The book, the engine and the gateway are single-threaded and do no I/O: no clock reads,
no randomness, no iteration over anything whose order depends on hashing. Their output
is a pure function of the sequence of requests they handle. Everything else follows:

- **Failures replay exactly.** A seed plus a step count reproduces any failure, however
  rare.
- **Recovery is replay.** Logging the inputs is enough; outputs never need to be stored.
- **A twin is an oracle.** A copy of the exchange that never crashes, fed the same
  logged requests, must behave identically. Any difference is a durability bug.
- **Implementations can be compared.** The same engine runs on the real order book or
  on the reference model, behind one `Book` protocol.

The cost is that the core uses one core. This is also how serious matching engines are
built (LMAX is the best-known example). Matching is inherently sequential per
instrument, and determinism is worth more than parallelism inside the hot path.
Instruments can be sharded across processes, each one still single-threaded.

## The order book

**Data structures.** Each price level is an intrusive doubly linked list of orders, and
a dict maps order IDs to list nodes. Adding at the back, filling from the front and
cancelling from the middle are all O(1). Level prices live in a sorted list keyed so
that the best price is always last (bids by `price`, asks by `-price`). Reading or
removing the best level is O(1), and one code path serves both sides.

**Trade-off.** Creating a new level, or removing one that is not the best, is
O(levels) because of the list shift. Real books have few active levels near the touch,
and most activity happens there, so this is cheap in practice. The benchmark's books
have about 280 levels each, and level maintenance is about 5% of the book's time in
the profile. A
skip list or a B-tree keyed by price would make this O(log levels) for deep books.

**A Python dict was considered, and rejected, as the FIFO queue.** Dicts preserve
insertion order and delete in O(1), but popping the front repeatedly leaves dead slots
that later iterations scan past. That gives quadratic behaviour in exactly the
fill-from-the-front pattern a book produces.

**Integers only.** Prices are ticks and quantities are lots. Floats would make
equality, aggregation and replay depend on rounding.

**Events are the interface.** Every command returns the full list of events it caused,
rather than the caller inspecting state afterwards. That is what lets two
implementations be compared exactly, lets an audit check the stream without looking
inside the book, and lets the gateway route reports.

**Self-trade prevention** is decided by the incoming order, as on CME Globex and
Deribit. The default is to cancel the resting order and keep matching, which is CME's
default: the incoming order is the trader's latest intent, and the resting one is
probably a stale quote. Other venues choose differently: Coinbase defaults to
decrement-and-cancel, and some venues allow self-trades unless a trader opts in. So all
three cancel modes are available per order. Orders ahead of the conflict still trade.

## The engine

- **Order IDs** come from one counter, so they are unique across symbols and never
  reused. A cancel can never hit a recycled ID.
- **Sequence numbers** on every event make the engine's whole output one totally
  ordered stream.
- **Cancels are checked for ownership, and a rejection reveals nothing.** Cancelling
  someone else's order gets exactly the same answer, with no symbol, as an ID that never
  existed, so a cancel cannot be used to discover other traders' orders.

## The gateway

The network may lose, duplicate, delay and reorder messages, and the exchange may
crash. Exactly-once *processing* is built from two pieces that are each easy to get
right: at-least-once delivery, and deduplication.

- **Requests:** clients name each one with their own client order ID (FIX's ClOrdID)
  and retry until they get a reply. The gateway remembers every (trader, client order
  ID) it has handled and ignores repeats. Reusing an ID for a *different* request is
  rejected, not silently ignored. Cancels name the client order ID, so a client can
  cancel an order whose acknowledgment it never received.
- **Reports:** each one carries a per-trader sequence number. Clients process reports
  strictly in order, drop duplicates, and buffer early arrivals. Every tick they send
  `ResendRequest(next_expected)`. That recovers any gap, including losses at the tail
  where no later report would reveal one. It also acknowledges everything earlier, so
  the gateway can discard it (FIX's gap fill).
- **Replies are explicit.** Every request gets at least one report with `in_reply_to`
  set, which is how a client knows to stop retrying. A fill on a resting order goes to
  its owner with `in_reply_to=None`.
- **No spoofing.** An order whose spec names another trader is rejected.

**Known limitation:** the deduplication table keeps every request forever. A real venue
scopes client order IDs to a session or trading day, then expires them.

## Persistence and recovery

1. Each request is appended to the log *before* it is applied (write-ahead).
2. Periodically, the exchange stores an immutable snapshot with the log position it
   covers.
3. Recovery loads the latest snapshot and replays the log after it, discarding the
   regenerated reports. Those were either delivered before the crash or will be
   resent.

A crash between steps 1 and 3 is the dangerous window, and it is safe for a specific
reason. Recovery applies the logged request, and the client's retry is then recognised
as a duplicate. A crash *before* the append loses the request, which is also safe,
because the exchange never acted on it. Replaying a request twice is harmless because
handling is idempotent; the planted `recovery-skips-last` bug shows what replaying too
little does.

Snapshots are built from immutable state: book snapshots are tuples of frozen records,
not object graphs. Restoring rebuilds the data structures from them. This keeps
snapshots independent of the in-memory layout and avoids deep recursion through the
linked lists.

**Simplifications:** `Storage` stands in for a disk and keeps Python objects. A real
implementation would serialise to bytes, batch log writes with an explicit fsync
policy, checksum entries to detect torn writes, and truncate the log behind snapshots.

## The simulator

- **Time** is an integer count of simulated microseconds that advances only when the
  scheduler pops the next action. Actions due at the same time run in the order they
  were scheduled.
- **Randomness:** each component has its own stream, `Random(f"{seed}/{name}")`.
  Adding a trader does not shift every other component's choices. String seeds are
  hashed with SHA-512 inside `random.Random`, not with Python's per-process salted
  `hash`, so streams are identical in every process.
- **Agents** only learn what happened from the reports they receive, like real clients.
  The mix is tuned to exercise every path. A test fails if a run stops producing any
  kind of outcome, because a simulator that silently tests less is worse than none.
- **Faults:** 5% message loss, 5% duplication, latencies of 10–600 µs (so messages
  reorder), periodic crashes, and crashes between logging and applying a request.
- **Digest:** every logged request, report, crash and recovery feeds a SHA-256 digest.
  A test runs the same seed in subprocesses with different `PYTHONHASHSEED` values and
  requires identical digests.

### The oracles

Each catches what the others cannot:

| Oracle | Strength | Blind spot |
| --- | --- | --- |
| Reference model | Exact expected output for any input | Code it shares with the book (e.g. `OrderSpec.accepts`); the engine layer, which is the same code on both sides |
| Audit | Independent of any implementation | Says nothing about priority order |
| Structural invariants | Catch corruption at the moment it happens | Cannot judge whether behaviour is right |
| Twin exchange | Any durability or recovery bug | Bugs that also exist in the twin, i.e. in the engine itself, which is why the twin's engine runs under all the checks above |
| Liveness watchdog | Stalls, lost restarts | Bounded only by its 100 ms limit |
| End-to-end convergence | Anything that corrupts a client's view of its orders | Runs only at the end |

Each blind spot was found the hard way, and each is covered by another row. The weakest
known one: a bug in code shared by the book and the reference model is invisible to
the comparison, and is caught only if it breaks an invariant or an audit rule. A
planted off-by-one in the crossing rule, for example, was caught by "the book is never
crossed".

## Performance

Pure Python, untuned. The profile is flat: the cost is object allocation (frozen
dataclasses for every event and report) and attribute access, not algorithmic. The
gateway's tail latency is mostly garbage-collection pauses, because the benchmark lets
unacknowledged reports accumulate. A faster version would, in order:

1. Allocate less on the hot path: reuse event objects or encode events as tuples or
   structs, and call `gc.freeze()` after warm-up.
2. Port the book and engine to a compiled language, which is what the optional M6
   milestone was for. The reference model, the audit and every seed would stay the
   same and verify the port. That is the real payoff of building the checks first.

## Not in scope

Real sockets and FIX encoding, market data feeds, multithreading, risk checks,
cancel-replace, iceberg and post-only orders, auctions. The point of the project is
correctness and determinism under failure, not breadth of features.
