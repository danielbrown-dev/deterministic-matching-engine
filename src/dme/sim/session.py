"""The client side of the gateway protocol.

A session turns an unreliable network into exactly-once, in-order delivery:

  * every request gets a fresh client order ID and is retried until a report
    replying to it arrives (retries are safe: the gateway ignores duplicates);
  * reports are processed strictly in sequence: duplicates are dropped, early
    arrivals wait in a buffer until the gap before them is filled;
  * every tick it sends ``ResendRequest(next_expected)``, which recovers lost
    reports, including at the tail where no later report would reveal a gap,
    and acknowledges everything already processed.
"""

from __future__ import annotations

from collections.abc import Callable

from dme.core import OrderSpec, Symbol, TraderId
from dme.gateway import (
    CancelRequest,
    ClientOrderId,
    NewOrderRequest,
    Report,
    Request,
    ResendRequest,
)


class ClientSession:
    def __init__(
        self,
        trader: TraderId,
        send: Callable[[Request], None],
        deliver: Callable[[Report], None],
        clock: Callable[[], int],
    ) -> None:
        self.trader = trader
        self._send = send
        self._deliver = deliver
        self._clock = clock
        self._issued_at: dict[ClientOrderId, int] = {}
        self._next_id = 1
        self.outstanding: dict[ClientOrderId, NewOrderRequest | CancelRequest] = {}
        self.next_expected = 1
        self._early: dict[int, Report] = {}

    def place(self, symbol: Symbol, spec: OrderSpec) -> ClientOrderId:
        request = NewOrderRequest(self.trader, self._new_id(), symbol, spec)
        self._issue(request)
        return request.client_order_id

    def cancel(self, target: ClientOrderId) -> ClientOrderId:
        request = CancelRequest(self.trader, self._new_id(), target)
        self._issue(request)
        return request.client_order_id

    @property
    def last_issued_id(self) -> int:
        return self._next_id - 1

    def receive(self, report: Report) -> None:
        if report.seq < self.next_expected or report.seq in self._early:
            return  # duplicate
        self._early[report.seq] = report
        while self.next_expected in self._early:
            ready = self._early.pop(self.next_expected)
            self.next_expected += 1
            if ready.in_reply_to is not None:
                self.outstanding.pop(ready.in_reply_to, None)
                self._issued_at.pop(ready.in_reply_to, None)
            self._deliver(ready)

    def tick(self) -> None:
        """Retry unanswered requests and ask for anything not yet received."""
        for request in list(self.outstanding.values()):
            self._send(request)
        self._send(ResendRequest(self.trader, self.next_expected))

    def oldest_unanswered_age(self) -> int:
        """Simulated microseconds the oldest unanswered request has waited (0 if none)."""
        if not self._issued_at:
            return 0
        return self._clock() - min(self._issued_at.values())

    @property
    def idle(self) -> bool:
        return not self.outstanding and not self._early

    def _new_id(self) -> ClientOrderId:
        client_order_id = ClientOrderId(self._next_id)
        self._next_id += 1
        return client_order_id

    def _issue(self, request: NewOrderRequest | CancelRequest) -> None:
        self.outstanding[request.client_order_id] = request
        self._issued_at[request.client_order_id] = self._clock()
        self._send(request)
