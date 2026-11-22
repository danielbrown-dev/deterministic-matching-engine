"""Deterministic simulation runs.

A run is a pure function of ``(seed, steps, config)``. Agents wake on a simulated
clock, send commands to the engine, and learn the results from its events. After
every command the ``EngineChecker`` verifies every invariant. Any failure, whether
a broken invariant or a crash, is reported with the seed and step that reproduce it.

Every command and event is fed into a SHA-256 digest. Two runs with the same inputs
must produce the same digest, in any process, on any machine. That is the
determinism check: if it ever fails, something outside the seed leaked into the run.
"""

from __future__ import annotations

import hashlib
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass

from dme.core import Symbol, TraderId
from dme.engine import Command, Sequenced
from dme.sim.agents import Agent, Market, MarketMaker, RandomTrader, Taker
from dme.sim.checker import EngineChecker
from dme.sim.scheduler import Scheduler, rng_stream

TRACE_LENGTH = 20


@dataclass(frozen=True)
class SimConfig:
    symbols: tuple[str, ...] = ("AAPL", "MSFT", "GOOG")
    market_makers: int = 2
    random_traders: int = 4
    takers: int = 2
    initial_price: int = 1_000
    fair_value_interval: int = 100
    """Simulated microseconds between fair-value moves."""
    reference: bool = False
    """Also run an engine on reference books and require identical output. Thorough
    but slow: the reference model scans every resting order on every command."""


@dataclass(frozen=True)
class SimResult:
    seed: int
    steps: int
    sim_time: int
    digest: str
    event_counts: Mapping[str, int]


@dataclass(frozen=True)
class TraceEntry:
    step: int
    time: int
    command: Command


class SimulationFailure(Exception):
    def __init__(
        self,
        seed: int,
        step: int,
        time: int,
        config: SimConfig,
        trace: list[TraceEntry],
        cause: BaseException,
    ) -> None:
        self.seed = seed
        self.step = step
        self.time = time
        self.config = config
        self.trace = trace
        self.cause = cause
        super().__init__(str(self))

    @property
    def repro_command(self) -> str:
        flags = " --reference" if self.config.reference else ""
        return f"python -m dme.sim --seed {self.seed} --steps {self.step + 1}{flags}"

    def __str__(self) -> str:
        lines = [
            f"seed={self.seed} failed at step {self.step} (t={self.time}us): "
            f"{type(self.cause).__name__}: {self.cause}",
            f"reproduce with: {self.repro_command}",
            f"last {len(self.trace)} commands:",
        ]
        lines += [f"  step {e.step} t={e.time}us {e.command}" for e in self.trace]
        return "\n".join(lines)


class Simulation:
    def __init__(self, seed: int, config: SimConfig) -> None:
        self.seed = seed
        self.config = config
        symbols = tuple(Symbol(s) for s in config.symbols)
        self.scheduler = Scheduler()
        self.market = Market(symbols, config.initial_price, rng_stream(seed, "market"))
        self.checker = EngineChecker(symbols, reference=config.reference)
        self.agents = self._make_agents()
        self.steps = 0
        self._limit = 0
        self._digest = hashlib.sha256()
        self._counts: Counter[str] = Counter()
        self._trace: deque[TraceEntry] = deque(maxlen=TRACE_LENGTH)

        self.scheduler.after(config.fair_value_interval, self._move_market)
        for agent in self.agents:
            self._wake_later(agent)

    def run(self, steps: int) -> SimResult:
        self._limit = steps
        while self.steps < steps:
            if not self.scheduler.run_next():
                raise RuntimeError("scheduler ran dry")  # agents always reschedule
        return SimResult(
            seed=self.seed,
            steps=self.steps,
            sim_time=self.scheduler.now,
            digest=self._digest.hexdigest(),
            event_counts=dict(sorted(self._counts.items())),
        )

    def _make_agents(self) -> list[Agent]:
        kinds: list[tuple[str, type[Agent], int]] = [
            ("maker", MarketMaker, self.config.market_makers),
            ("random", RandomTrader, self.config.random_traders),
            ("taker", Taker, self.config.takers),
        ]
        agents: list[Agent] = []
        for name, kind, count in kinds:
            for i in range(count):
                trader = TraderId(len(agents) + 1)
                rng = rng_stream(self.seed, f"{name}/{i}")
                agents.append(kind(trader, rng, self.market))
        return agents

    def _move_market(self) -> None:
        self.market.step()
        self.scheduler.after(self.config.fair_value_interval, self._move_market)

    def _wake_later(self, agent: Agent) -> None:
        self.scheduler.after(agent.next_delay(), lambda: self._wake(agent))

    def _wake(self, agent: Agent) -> None:
        for command in agent.act():
            if self.steps >= self._limit:
                return
            self._apply(command)
        self._wake_later(agent)

    def _apply(self, command: Command) -> None:
        self._trace.append(TraceEntry(self.steps, self.scheduler.now, command))
        try:
            published = self.checker.apply(command)
        except Exception as exc:
            raise SimulationFailure(
                self.seed, self.steps, self.scheduler.now, self.config, list(self._trace), exc
            ) from exc
        self.steps += 1
        self._record(command, published)
        self.market.observe(published)
        for agent in self.agents:
            agent.observe(published)

    def _record(self, command: Command, published: list[Sequenced]) -> None:
        self._digest.update(f"{command!r}\n".encode())
        for s in published:
            self._digest.update(f"{s!r}\n".encode())
            reason = getattr(s.event, "reason", None)
            key = type(s.event).__name__ + (f":{reason.name}" if reason is not None else "")
            self._counts[key] += 1


def run(seed: int, steps: int, config: SimConfig | None = None) -> SimResult:
    """Run one simulation. Raises ``SimulationFailure`` on any violation or crash."""
    return Simulation(seed, config or SimConfig()).run(steps)
