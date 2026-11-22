"""Deterministic simulation runs.

A run is a pure function of ``(seed, steps, config)``. Agents wake on a simulated
clock and trade through client sessions; requests and reports cross a simulated
network; the exchange node logs every request before applying it, and may crash and
recover. ``steps`` counts requests durably logged by the exchange.

Checks, all raising on the first failure:
  * every engine command, via the ``EngineChecker`` on the shadow exchange;
  * the live exchange sends exactly the shadow's reports, and equals the shadow's
    state after every recovery (see ``dme.sim.node``);
  * exactly once: no (trader, client order ID) is ever accepted twice;
  * every report a client processes is exactly the next one in its trader's stream;
  * at the end, faults stop, the network drains, and every client's view of its
    resting orders must match the exchange exactly.

Failures report the seed, the log position, the last requests logged and an exact
repro command. Every logged request, report, crash and recovery feeds a SHA-256
digest of the run: the same inputs must give the same digest in any process.
"""

from __future__ import annotations

import hashlib
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

from dme.core import InvariantViolation, Side, Symbol, TraderId
from dme.events import Accepted
from dme.gateway import ClientKey, ClientOrderId, GatewayReject, Report, Request
from dme.sim.agents import Agent, Market, MarketMaker, RandomTrader, Taker
from dme.sim.network import FAULT_PRESETS, Faults, Network
from dme.sim.node import ExchangeNode
from dme.sim.scheduler import Scheduler, rng_stream
from dme.sim.session import ClientSession

TRACE_LENGTH = 20
DRAIN_TIME_LIMIT = 5_000_000
LIVENESS_LIMIT = 100_000
"""Simulated microseconds a request may go unanswered before the run fails. Far above
the worst legitimate wait (a 5 ms outage plus a few retries), so exceeding it means
clients are not being served: a liveness bug, which no safety check would catch."""


@dataclass(frozen=True)
class SimConfig:
    symbols: tuple[str, ...] = ("AAPL", "MSFT", "GOOG")
    market_makers: int = 2
    random_traders: int = 4
    takers: int = 2
    initial_price: int = 1_000
    fair_value_interval: int = 100
    """Simulated microseconds between fair-value moves."""
    tick_interval: int = 2_000
    """Simulated microseconds between a session's retries and resend requests."""
    snapshot_every: int = 500
    """Log entries between exchange snapshots."""
    faults: str = "none"
    """A key of ``FAULT_PRESETS``: none, net, crash or all."""
    reference: bool = False
    """Also run the shadow's engine against reference books and require identical
    output. Thorough but slow: the reference model scans every resting order."""


@dataclass(frozen=True)
class SimResult:
    seed: int
    steps: int
    sim_time: int
    digest: str
    counts: Mapping[str, int]


class SimulationFailure(Exception):
    def __init__(
        self,
        seed: int,
        step: int,
        repro_steps: int,
        time: int,
        config: SimConfig,
        trace: list[tuple[int, int, Request]],
        cause: BaseException,
    ) -> None:
        self.seed = seed
        self.step = step
        self.repro_steps = repro_steps
        self.time = time
        self.config = config
        self.trace = trace
        self.cause = cause
        super().__init__(str(self))

    @property
    def repro_command(self) -> str:
        flags = f" --faults {self.config.faults}" if self.config.faults != "none" else ""
        flags += " --reference" if self.config.reference else ""
        return f"python -m dme.sim --seed {self.seed} --steps {self.repro_steps}{flags}"

    def __str__(self) -> str:
        lines = [
            f"seed={self.seed} failed at log position {self.step} (t={self.time}us): "
            f"{type(self.cause).__name__}: {self.cause}",
            f"reproduce with: {self.repro_command}",
            f"last {len(self.trace)} requests logged:",
        ]
        lines += [f"  #{position} t={time}us {request}" for position, time, request in self.trace]
        return "\n".join(lines)


class Simulation:
    def __init__(self, seed: int, config: SimConfig) -> None:
        self.seed = seed
        self.config = config
        self.faults = FAULT_PRESETS[config.faults]
        symbols = tuple(Symbol(s) for s in config.symbols)
        self.scheduler = Scheduler()
        self.network = Network(self.scheduler, rng_stream(seed, "network"), self.faults)
        self.market = Market(symbols, config.initial_price, rng_stream(seed, "market"))
        self.node = ExchangeNode(
            symbols,
            reference=config.reference,
            snapshot_every=config.snapshot_every,
            crash_mid_request=self.faults.crash_mid_request,
            rng=rng_stream(seed, "node"),
            on_logged=self._on_logged,
            on_crash=self._on_crash,
        )
        self.sessions: dict[TraderId, ClientSession] = {}
        self._agents_by_trader: dict[TraderId, Agent] = {}
        self.agents = self._make_agents()
        self.draining = False

        self._crash_rng = rng_stream(seed, "crashes")
        self._digest = hashlib.sha256()
        self._counts: Counter[str] = Counter()
        self._trace: deque[tuple[int, int, Request]] = deque(maxlen=TRACE_LENGTH)
        self._reports: dict[TraderId, list[Report]] = {}  # every report, per trader
        self._processed: Counter[TraderId] = Counter()  # reports each client processed
        self._accepted: set[ClientKey] = set()

        self.scheduler.after(config.fair_value_interval, self._move_market)
        for agent in self.agents:
            self._wake_later(agent)
        tick_rng = rng_stream(seed, "ticks")
        for session in self.sessions.values():
            self.scheduler.after(tick_rng.randint(1, config.tick_interval), self._ticker(session))
        if self.faults.crash_interval is not None:
            self.scheduler.after(self._crash_rng.randint(*self.faults.crash_interval), self._crash)

    @property
    def steps(self) -> int:
        return len(self.node.storage.log)

    def run(self, steps: int) -> SimResult:
        try:
            while self.steps < steps:
                if not self.scheduler.run_next():
                    raise RuntimeError("scheduler ran dry")  # agents always reschedule
        except Exception as exc:
            raise self._failure(exc, repro_steps=self.steps + 1) from exc
        try:
            self._drain_and_verify()
        except Exception as exc:
            raise self._failure(exc, repro_steps=steps) from exc

        self._counts["net:sent"] = self.network.sent
        self._counts["net:dropped"] = self.network.dropped
        self._counts["net:duplicated"] = self.network.duplicated
        self._counts["node:crashes"] = self.node.crashes
        self._counts["node:mid_request_crashes"] = self.node.mid_request_crashes
        self._counts["node:lost_while_down"] = self.node.lost_while_down
        return SimResult(
            seed=self.seed,
            steps=self.steps,
            sim_time=self.scheduler.now,
            digest=self._digest.hexdigest(),
            counts=dict(sorted(self._counts.items())),
        )

    # Setup

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
                session = ClientSession(
                    trader,
                    send=self._send_to_exchange,
                    deliver=self._deliver,
                    clock=lambda: self.scheduler.now,
                )
                agent = kind(trader, rng_stream(self.seed, f"{name}/{i}"), self.market, session)
                self.sessions[trader] = session
                self._agents_by_trader[trader] = agent
                agents.append(agent)
        return agents

    # Scheduled actions

    def _move_market(self) -> None:
        self.market.step()
        self.scheduler.after(self.config.fair_value_interval, self._move_market)

    def _wake_later(self, agent: Agent) -> None:
        self.scheduler.after(agent.next_delay(), lambda: self._wake(agent))

    def _wake(self, agent: Agent) -> None:
        if not self.draining:
            agent.act()
            self._wake_later(agent)

    def _ticker(self, session: ClientSession) -> Callable[[], None]:
        def tick() -> None:
            age = session.oldest_unanswered_age()
            if age > LIVENESS_LIMIT:
                raise InvariantViolation(
                    f"trader {session.trader} has waited {age}us for a reply: not being served"
                )
            session.tick()
            self.scheduler.after(self.config.tick_interval, tick)

        return tick

    def _crash(self) -> None:
        """The periodic crash process."""
        if self.draining:
            return
        self.node.crash()
        assert self.faults.crash_interval is not None
        self.scheduler.after(self._crash_rng.randint(*self.faults.crash_interval), self._crash)

    def _on_crash(self) -> None:
        """Every crash, periodic or mid-request, schedules a restart."""
        self._digest.update(f"crash t={self.scheduler.now}\n".encode())
        self.scheduler.after(self._crash_rng.randint(*self.faults.downtime), self._restart)

    def _restart(self) -> None:
        if not self.node.up:
            self.node.restart()
            self._digest.update(f"restart t={self.scheduler.now}\n".encode())
            self._counts["node:recoveries"] += 1

    # Message flow

    def _send_to_exchange(self, request: Request) -> None:
        self.network.send(partial(self._exchange_receives, request))

    def _exchange_receives(self, request: Request) -> None:
        for report in self.node.receive(request):
            self.network.send(partial(self.sessions[report.trader].receive, report))

    def _on_logged(self, request: Request, reports: list[Report]) -> None:
        """Called by the node for every durably logged request, with the shadow's
        (correct) reports for it."""
        self._trace.append((self.steps - 1, self.scheduler.now, request))
        self._digest.update(f"{request!r}\n".encode())
        self._counts[f"request:{type(request).__name__}"] += 1
        for report in reports:
            stream = self._reports.setdefault(report.trader, [])
            if report.seq <= len(stream):
                continue  # a resend, already recorded
            if report.seq != len(stream) + 1:
                raise InvariantViolation(f"report sequence gap for trader {report.trader}")
            stream.append(report)
            self._digest.update(f"{report!r}\n".encode())
            self._count(report)
            if isinstance(report.event, Accepted):
                key = (report.trader, report.client_order_id)
                if key in self._accepted:
                    raise InvariantViolation(f"client order {key} accepted twice")
                self._accepted.add(key)

    def _deliver(self, report: Report) -> None:
        """A session hands a report to its agent: it must be exactly the next report in
        that trader's stream. Not merely a correct report: a duplicate is correct too."""
        stream = self._reports.get(report.trader, [])
        index = self._processed[report.trader]
        if index >= len(stream) or stream[index] != report:
            raise InvariantViolation(
                f"client {report.trader} processed {report}, expected report #{index + 1}"
            )
        self._processed[report.trader] += 1
        self._agents_by_trader[report.trader].observe(report)

    def _count(self, report: Report) -> None:
        event = report.event
        if isinstance(event, GatewayReject):
            self._counts[f"GatewayReject:{event.reason.name}"] += 1
            return
        reason = getattr(event, "reason", None)
        key = type(event).__name__ + (f":{reason.name}" if reason is not None else "")
        self._counts[key] += 1

    # End of run

    def _drain_and_verify(self) -> None:
        """Stop trading and faults, let every session catch up, then require every
        client's view of its resting orders to match the exchange exactly."""
        self.draining = True
        self.network.faults = Faults()
        self.node.crash_mid_request = 0.0
        self._restart()
        live = self.node.live
        assert live is not None
        deadline = self.scheduler.now + DRAIN_TIME_LIMIT
        while not self._converged():
            if self.scheduler.now > deadline:
                raise InvariantViolation("clients did not catch up after faults stopped")
            self.scheduler.run_next()

        for agent in self.agents:
            exchange_view: dict[ClientOrderId, tuple[Symbol, Side, int, int]] = {}
            for symbol in self.node.symbols:
                snapshot = live.engine.snapshot(symbol)
                for order in snapshot.bids + snapshot.asks:
                    trader, cid = live.client_key(order.order_id)
                    if trader == agent.trader:
                        exchange_view[cid] = (symbol, order.side, order.price, order.qty)
            client_view = {cid: (o.symbol, o.side, o.price, o.qty) for cid, o in agent.live.items()}
            if client_view != exchange_view:
                raise InvariantViolation(
                    f"trader {agent.trader} believes {client_view}, exchange has {exchange_view}"
                )

    def _converged(self) -> bool:
        live = self.node.live
        assert live is not None
        return all(
            session.idle and session.next_expected == live.next_report_seq(trader)
            for trader, session in self.sessions.items()
        )

    def _failure(self, exc: BaseException, repro_steps: int) -> SimulationFailure:
        return SimulationFailure(
            self.seed,
            self.steps,
            repro_steps,
            self.scheduler.now,
            self.config,
            list(self._trace),
            exc,
        )


def run(seed: int, steps: int, config: SimConfig | None = None) -> SimResult:
    """Run one simulation. Raises ``SimulationFailure`` on any violation or crash."""
    return Simulation(seed, config or SimConfig()).run(steps)
