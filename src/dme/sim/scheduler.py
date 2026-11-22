"""Simulated time and a deterministic event queue.

Nothing in the simulator reads the wall clock. Time is an integer count of simulated
microseconds that only moves when the scheduler pops the next action. Actions due at
the same time run in the order they were scheduled, so a run never depends on how
the heap breaks ties.
"""

from __future__ import annotations

import heapq
import random
from collections.abc import Callable

Action = Callable[[], None]


class Scheduler:
    def __init__(self) -> None:
        self.now = 0
        self._queue: list[tuple[int, int, Action]] = []
        self._scheduled = 0  # tie-breaker: insertion order

    def at(self, time: int, action: Action) -> None:
        if time < self.now:
            raise ValueError(f"cannot schedule at {time}, before now ({self.now})")
        heapq.heappush(self._queue, (time, self._scheduled, action))
        self._scheduled += 1

    def after(self, delay: int, action: Action) -> None:
        self.at(self.now + delay, action)

    def run_next(self) -> bool:
        """Advance time to the next action and run it. Returns False if none is left."""
        if not self._queue:
            return False
        time, _, action = heapq.heappop(self._queue)
        self.now = time
        action()
        return True

    def __len__(self) -> int:
        return len(self._queue)


def rng_stream(seed: int, name: str) -> random.Random:
    """An independent random stream for one component of a run.

    Each component draws from its own stream, so adding a trader or changing how
    often one acts does not shift every other component's random choices. Seeding
    with a string is deterministic across processes: ``random.Random`` hashes str
    seeds with SHA-512, not with Python's per-process salted ``hash``.
    """
    return random.Random(f"{seed}/{name}")
