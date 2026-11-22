"""The client side of the protocol, tested against hand-crafted report streams."""

from __future__ import annotations

from dme.core import OrderId, OrderSpec, Side, Symbol, TraderId
from dme.events import Accepted, Cancelled, CancelReason
from dme.gateway import ClientOrderId, NewOrderRequest, Report, Request, ResendRequest
from dme.sim.session import ClientSession

ALICE = TraderId(1)
AAPL = Symbol("AAPL")


class Harness:
    def __init__(self) -> None:
        self.now = 0
        self.sent: list[Request] = []
        self.delivered: list[int] = []
        self.session = ClientSession(
            ALICE,
            send=self.sent.append,
            deliver=lambda r: self.delivered.append(r.seq),
            clock=lambda: self.now,
        )


def report(seq: int, in_reply_to: int | None = None) -> Report:
    reply = None if in_reply_to is None else ClientOrderId(in_reply_to)
    event = Cancelled(OrderId(seq), 1, CancelReason.REQUESTED)
    return Report(ALICE, seq, ClientOrderId(seq), reply, event)


def test_duplicates_are_dropped() -> None:
    h = Harness()
    for seq in (1, 1, 2, 1, 2, 3):
        h.session.receive(report(seq))
    assert h.delivered == [1, 2, 3]


def test_out_of_order_reports_wait_for_the_gap() -> None:
    h = Harness()
    for seq in (3, 2, 5):
        h.session.receive(report(seq))
    assert h.delivered == []
    h.session.receive(report(1))
    assert h.delivered == [1, 2, 3]
    h.session.receive(report(4))
    assert h.delivered == [1, 2, 3, 4, 5]


def test_requests_are_retried_until_answered() -> None:
    h = Harness()
    cid = h.session.place(AAPL, OrderSpec.limit(ALICE, Side.BUY, 100, 1))
    assert isinstance(h.sent[0], NewOrderRequest)
    h.sent.clear()
    h.session.tick()
    assert h.sent == [h.session.outstanding[cid], ResendRequest(ALICE, 1)]

    accepted = Accepted(OrderId(1), OrderSpec.limit(ALICE, Side.BUY, 100, 1))
    h.session.receive(Report(ALICE, 1, cid, cid, accepted))
    h.sent.clear()
    h.session.tick()
    assert h.sent == [ResendRequest(ALICE, 2)]  # no retry: it was answered
    assert h.session.idle


def test_reports_not_addressed_as_replies_do_not_clear_requests() -> None:
    h = Harness()
    h.session.place(AAPL, OrderSpec.limit(ALICE, Side.BUY, 100, 1))
    h.session.receive(report(1, in_reply_to=None))  # e.g. a fill on another order
    assert not h.session.idle


def test_waiting_time_of_unanswered_requests() -> None:
    h = Harness()
    assert h.session.oldest_unanswered_age() == 0
    h.now = 100
    h.session.place(AAPL, OrderSpec.limit(ALICE, Side.BUY, 100, 1))
    h.now = 450
    assert h.session.oldest_unanswered_age() == 350
