"""The client-facing gateway, and ``Exchange``: the gateway in front of the engine.

Clients talk to the exchange over a network that may drop, duplicate, delay and
reorder messages, and the exchange may crash. The gateway makes that safe:

Exactly-once order entry
    Clients name every request with their own *client order ID*, unique per trader
    (FIX's ClOrdID). A request whose (trader, client order ID) was already handled is
    a duplicate delivery or a client retry, and is ignored, so retrying is always
    safe: an order can never be entered twice. Reusing an ID for a different request
    is rejected.

Reliable, ordered reports
    Every report to a trader carries the next number in that trader's sequence,
    starting at 1. Clients process reports strictly in sequence, drop duplicates,
    and on a gap send ``ResendRequest(from_seq)``, which also acknowledges everything
    before ``from_seq`` so the gateway can discard it (FIX's gap fill).

Replies
    Every request gets at least one report addressed to its sender with
    ``in_reply_to`` set to the request's client order ID; that is how a client knows
    a request was handled and can stop retrying it. Reports caused by someone else,
    such as a fill on a resting order, have ``in_reply_to=None``.

``Exchange`` is deterministic, like the engine: its state is a pure function of the
requests it has handled, which is what makes log-replay recovery (``dme.persistence``)
possible.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import NewType

from dme.core import OrderId, OrderSpec, Symbol, TraderId
from dme.engine import CancelOrder, Command, Engine, EngineState, Sequenced, SubmitOrder
from dme.events import Accepted, Event, Rejected, Trade

ClientOrderId = NewType("ClientOrderId", int)
ClientKey = tuple[TraderId, ClientOrderId]


@dataclass(frozen=True, slots=True)
class NewOrderRequest:
    trader: TraderId
    client_order_id: ClientOrderId
    symbol: Symbol
    spec: OrderSpec


@dataclass(frozen=True, slots=True)
class CancelRequest:
    trader: TraderId
    client_order_id: ClientOrderId
    """This request's own ID, from the same per-trader space as order IDs."""
    target: ClientOrderId
    """The client order ID of the order to cancel."""


@dataclass(frozen=True, slots=True)
class ResendRequest:
    trader: TraderId
    from_seq: int
    """Resend every report from this sequence number on. Also acknowledges every
    earlier report, which the gateway may then discard."""


Request = NewOrderRequest | CancelRequest | ResendRequest


class GatewayRejectReason(enum.Enum):
    UNKNOWN_ORDER = "unknown_order"
    """A cancel names a client order ID this trader never submitted."""
    CLIENT_ORDER_ID_REUSED = "client_order_id_reused"
    """A different request already used this client order ID."""
    WRONG_TRADER = "wrong_trader"
    """The order's spec names a trader other than the sender."""


@dataclass(frozen=True, slots=True)
class GatewayReject:
    reason: GatewayRejectReason


@dataclass(frozen=True, slots=True)
class Report:
    trader: TraderId
    seq: int
    client_order_id: ClientOrderId
    """The order this report is about (for a gateway reject: the rejected request)."""
    in_reply_to: ClientOrderId | None
    event: Event | GatewayReject


@dataclass(frozen=True, slots=True)
class ExchangeState:
    """Everything needed to rebuild an exchange exactly. Immutable."""

    engine: EngineState
    requests: tuple[tuple[ClientKey, NewOrderRequest | CancelRequest], ...]
    orders: tuple[tuple[ClientKey, OrderId], ...]
    next_report: tuple[tuple[TraderId, int], ...]
    unacknowledged: tuple[tuple[TraderId, tuple[Report, ...]], ...]


class Exchange:
    def __init__(
        self, engine: Engine, apply: Callable[[Command], list[Sequenced]] | None = None
    ) -> None:
        """``apply`` defaults to ``engine.apply``; the simulator passes a checked
        version so every engine command is verified."""
        self._engine = engine
        self._apply = apply or engine.apply
        self._requests: dict[ClientKey, NewOrderRequest | CancelRequest] = {}
        self._orders: dict[ClientKey, OrderId] = {}
        self._keys: dict[OrderId, ClientKey] = {}
        self._next_report: dict[TraderId, int] = {}
        self._unacknowledged: dict[TraderId, list[Report]] = {}

    def handle(self, request: Request) -> list[Report]:
        """Handle one request. Returns the reports to send: new ones, or for a
        ``ResendRequest`` the unacknowledged ones."""
        if isinstance(request, ResendRequest):
            return self._resend(request)

        key = (request.trader, request.client_order_id)
        previous = self._requests.get(key)
        if previous is not None:
            if previous == request:
                return []  # duplicate delivery or retry: already handled
            return [self._reject(request, GatewayRejectReason.CLIENT_ORDER_ID_REUSED)]
        self._requests[key] = request

        if isinstance(request, NewOrderRequest):
            if request.spec.trader != request.trader:
                return [self._reject(request, GatewayRejectReason.WRONG_TRADER)]
            published = self._apply(SubmitOrder(request.symbol, request.spec))
            first = published[0].event
            assert isinstance(first, Accepted | Rejected)
            self._orders[key] = first.order_id
            self._keys[first.order_id] = key
            return self._route(request, published)

        target = self._orders.get((request.trader, request.target))
        if target is None:
            return [self._reject(request, GatewayRejectReason.UNKNOWN_ORDER)]
        return self._route(request, self._apply(CancelOrder(request.trader, target)))

    def client_key(self, order_id: OrderId) -> ClientKey:
        return self._keys[order_id]

    def next_report_seq(self, trader: TraderId) -> int:
        return self._next_report.get(trader, 1)

    @property
    def engine(self) -> Engine:
        return self._engine

    # Snapshots

    def state(self) -> ExchangeState:
        return ExchangeState(
            engine=self._engine.state(),
            requests=tuple(self._requests.items()),
            orders=tuple(self._orders.items()),
            next_report=tuple(self._next_report.items()),
            unacknowledged=tuple((t, tuple(rs)) for t, rs in self._unacknowledged.items()),
        )

    @classmethod
    def from_state(cls, state: ExchangeState) -> Exchange:
        exchange = cls(Engine.from_state(state.engine))
        exchange._requests = dict(state.requests)
        exchange._orders = dict(state.orders)
        exchange._keys = {order_id: key for key, order_id in state.orders}
        exchange._next_report = dict(state.next_report)
        exchange._unacknowledged = {t: list(rs) for t, rs in state.unacknowledged}
        return exchange

    # Internals

    def _resend(self, request: ResendRequest) -> list[Report]:
        pending = self._unacknowledged.get(request.trader, [])
        kept = [r for r in pending if r.seq >= request.from_seq]
        if kept:
            self._unacknowledged[request.trader] = kept
        else:
            self._unacknowledged.pop(request.trader, None)
        return list(kept)

    def _route(
        self, request: NewOrderRequest | CancelRequest, published: list[Sequenced]
    ) -> list[Report]:
        reports: list[Report] = []
        for s in published:
            event = s.event
            order_ids = (
                (event.maker_id, event.taker_id) if isinstance(event, Trade) else (event.order_id,)
            )
            for order_id in order_ids:
                trader, client_order_id = self._keys[order_id]
                reply = request.client_order_id if trader == request.trader else None
                reports.append(self._report(trader, client_order_id, reply, event))
        return reports

    def _reject(
        self, request: NewOrderRequest | CancelRequest, reason: GatewayRejectReason
    ) -> Report:
        cid = request.client_order_id
        return self._report(request.trader, cid, cid, GatewayReject(reason))

    def _report(
        self,
        trader: TraderId,
        client_order_id: ClientOrderId,
        in_reply_to: ClientOrderId | None,
        event: Event | GatewayReject,
    ) -> Report:
        seq = self._next_report.get(trader, 1)
        self._next_report[trader] = seq + 1
        report = Report(trader, seq, client_order_id, in_reply_to, event)
        self._unacknowledged.setdefault(trader, []).append(report)
        return report
