"""A simulated network that loses, duplicates, delays and reorders messages.

Every message gets an independent random latency, so messages overtake each other
whenever ``max_latency > min_latency``. The network draws the same random numbers
whatever the fault settings, so turning a fault off does not change the latencies
everything else sees.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from dme.sim.scheduler import Action, Scheduler


@dataclass(frozen=True)
class Faults:
    drop: float = 0.0
    """Probability that a message is lost."""
    duplicate: float = 0.0
    """Probability that a message is delivered twice."""
    min_latency: int = 50
    max_latency: int = 50
    crash_interval: tuple[int, int] | None = None
    """Range of simulated microseconds between exchange crashes; None for no crashes."""
    crash_mid_request: float = 0.0
    """Probability, per request, of crashing after the request is logged but before it
    is applied: the window a write-ahead log exists to handle."""
    downtime: tuple[int, int] = (500, 5_000)


FAULT_PRESETS: dict[str, Faults] = {
    "none": Faults(),
    "net": Faults(drop=0.05, duplicate=0.05, min_latency=10, max_latency=600),
    "crash": Faults(crash_interval=(20_000, 150_000), crash_mid_request=0.002),
    "all": Faults(
        drop=0.05,
        duplicate=0.05,
        min_latency=10,
        max_latency=600,
        crash_interval=(20_000, 150_000),
        crash_mid_request=0.002,
    ),
}


class Network:
    def __init__(self, scheduler: Scheduler, rng: random.Random, faults: Faults) -> None:
        self.scheduler = scheduler
        self.faults = faults
        self._rng = rng
        self.sent = 0
        self.dropped = 0
        self.duplicated = 0

    def send(self, deliver: Action) -> None:
        self.sent += 1
        lost = self._rng.random() < self.faults.drop
        doubled = self._rng.random() < self.faults.duplicate
        if lost:
            self.dropped += 1
            return
        copies = 2 if doubled else 1
        self.duplicated += copies - 1
        for _ in range(copies):
            latency = self._rng.randint(self.faults.min_latency, self.faults.max_latency)
            self.scheduler.after(latency, deliver)
