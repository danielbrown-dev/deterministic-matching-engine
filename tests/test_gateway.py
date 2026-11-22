"""Example-based tests for the gateway's protocol guarantees."""

from __future__ import annotations

import pytest

from dme.core import OrderSpec, Side, Symbol, TraderId
from dme.engine import Engine
from dme.events import Cancelled, CancelRejected, Trade
from dme.gateway import (
    CancelRequest,
    ClientOrderId,
    Exchange,
    GatewayReject,
    GatewayRejectReason,
    NewOrderRequest,
    Report,
    Request,
    ResendRequest,
)

AAPL = Symbol("AAPL")
ALICE, BOB = TraderId(1), TraderId(2)
BUY, SELL = Side.BUY, Side.SELL


def cid(n: int) -> ClientOrderId:
    return ClientOrderId(n)


def new_order(trader: TraderId, n: int, side: Side, price: int, qty: int) -> NewOrderRequest:
    return NewOrderRequest(trader, cid(n), AAPL, OrderSpec.limit(trader, side, price, qty))


@pytest.fixture
def exchange() -> Exchange:
    return Exchange(Engine([AAPL]))


def kinds(reports: list[Report]) -> list[str]:
    return [type(r.event).__name__ for r in reports]


def test_reports_are_sequenced_per_trader(exchange: Exchange) -> None:
    a = exchange.handle(new_order(ALICE, 1, SELL, 100, 5))
    b = exchange.handle(new_order(BOB, 1, BUY, 100, 2))
    c = exchange.handle(new_order(ALICE, 2, SELL, 101, 1))
    assert [(r.trader, r.seq) for r in a + b + c] == [
        (ALICE, 1),
        (ALICE, 2),  # Accepted, Rested
        (BOB, 1),  # Accepted
        (ALICE, 3),  # Trade: the maker's copy first,
        (BOB, 2),  # then the taker's
        (ALICE, 4),
        (ALICE, 5),  # Accepted, Rested
    ]


def test_both_sides_of_a_trade_are_told_with_their_own_ids(exchange: Exchange) -> None:
    exchange.handle(new_order(ALICE, 7, SELL, 100, 5))
    reports = exchange.handle(new_order(BOB, 3, BUY, 100, 2))
    trades = [r for r in reports if isinstance(r.event, Trade)]
    assert {(r.trader, r.client_order_id, r.in_reply_to) for r in trades} == {
        (BOB, cid(3), cid(3)),  # the taker: a reply to its request
        (ALICE, cid(7), None),  # the maker: caused by someone else
    }


def test_duplicate_request_is_ignored(exchange: Exchange) -> None:
    request = new_order(ALICE, 1, BUY, 100, 5)
    first = exchange.handle(request)
    assert kinds(first) == ["Accepted", "Rested"]
    assert exchange.handle(request) == []
    assert len(exchange.engine.snapshot(AAPL).bids) == 1


def test_reusing_a_client_order_id_is_rejected(exchange: Exchange) -> None:
    exchange.handle(new_order(ALICE, 1, BUY, 100, 5))
    reports = exchange.handle(new_order(ALICE, 1, BUY, 99, 5))
    assert [r.event for r in reports] == [GatewayReject(GatewayRejectReason.CLIENT_ORDER_ID_REUSED)]


def test_cannot_submit_as_another_trader(exchange: Exchange) -> None:
    spec = OrderSpec.limit(BOB, BUY, 100, 5)
    reports = exchange.handle(NewOrderRequest(ALICE, cid(1), AAPL, spec))
    assert [(r.trader, r.event) for r in reports] == [
        (ALICE, GatewayReject(GatewayRejectReason.WRONG_TRADER))
    ]
    assert exchange.engine.snapshot(AAPL).bids == ()


def test_cancel_by_client_order_id(exchange: Exchange) -> None:
    exchange.handle(new_order(ALICE, 1, BUY, 100, 5))
    reports = exchange.handle(CancelRequest(ALICE, cid(2), target=cid(1)))
    (report,) = reports
    assert isinstance(report.event, Cancelled)
    assert (report.client_order_id, report.in_reply_to) == (cid(1), cid(2))


def test_cancel_of_unknown_client_order_id(exchange: Exchange) -> None:
    reports = exchange.handle(CancelRequest(ALICE, cid(5), target=cid(99)))
    assert [(r.client_order_id, r.in_reply_to, r.event) for r in reports] == [
        (cid(5), cid(5), GatewayReject(GatewayRejectReason.UNKNOWN_ORDER))
    ]


def test_cancel_after_fill_is_rejected_by_the_engine(exchange: Exchange) -> None:
    exchange.handle(new_order(ALICE, 1, SELL, 100, 5))
    exchange.handle(new_order(BOB, 1, BUY, 100, 5))
    (report,) = exchange.handle(CancelRequest(ALICE, cid(2), target=cid(1)))
    assert isinstance(report.event, CancelRejected)
    assert report.in_reply_to == cid(2)


def test_client_ids_are_per_trader(exchange: Exchange) -> None:
    """Alice and Bob may both use client order ID 1; Bob cannot cancel Alice's."""
    exchange.handle(new_order(ALICE, 1, BUY, 100, 5))
    exchange.handle(new_order(BOB, 1, BUY, 99, 5))
    exchange.handle(CancelRequest(BOB, cid(2), target=cid(1)))
    bids = exchange.engine.snapshot(AAPL).bids
    assert [order.trader for order in bids] == [ALICE]


def test_resend_returns_unacknowledged_reports_and_discards_the_rest(
    exchange: Exchange,
) -> None:
    exchange.handle(new_order(ALICE, 1, BUY, 100, 5))  # seq 1, 2
    exchange.handle(new_order(ALICE, 2, BUY, 99, 5))  # seq 3, 4
    assert [r.seq for r in exchange.handle(ResendRequest(ALICE, 3))] == [3, 4]
    assert [r.seq for r in exchange.handle(ResendRequest(ALICE, 1))] == [3, 4]  # 1, 2 gone
    assert exchange.handle(ResendRequest(ALICE, 5)) == []


def test_every_request_gets_a_reply(exchange: Exchange) -> None:
    requests: list[NewOrderRequest | CancelRequest] = [
        new_order(ALICE, 1, BUY, 100, 5),
        new_order(ALICE, 2, BUY, 0, 5),  # rejected by the engine
        CancelRequest(ALICE, cid(3), target=cid(1)),
        CancelRequest(ALICE, cid(4), target=cid(1)),  # already cancelled
        CancelRequest(ALICE, cid(5), target=cid(42)),  # unknown
    ]
    for request in requests:
        replies = [r for r in exchange.handle(request) if r.in_reply_to == request.client_order_id]
        assert replies, f"no reply to {request}"


def test_state_round_trips(exchange: Exchange) -> None:
    exchange.handle(new_order(ALICE, 1, SELL, 100, 5))
    exchange.handle(new_order(BOB, 1, BUY, 100, 2))
    exchange.handle(new_order(BOB, 2, BUY, 98, 3))
    restored = Exchange.from_state(exchange.state())
    assert restored.state() == exchange.state()
    # And it behaves identically afterwards.
    later: list[Request] = [new_order(BOB, 3, BUY, 100, 3), CancelRequest(BOB, cid(4), cid(2))]
    for request in later:
        assert restored.handle(request) == exchange.handle(request)
    assert restored.state() == exchange.state()
