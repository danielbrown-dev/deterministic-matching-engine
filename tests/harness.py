"""Shared differential-testing step: run one command on the real book and the
reference model, and check that they agree and that every invariant holds."""

from __future__ import annotations

from dataclasses import dataclass

from dme.audit import Audit
from dme.book import OrderBook
from dme.core import OrderId, Price, Qty, Side
from dme.events import Event
from dme.reference import ReferenceBook


@dataclass(frozen=True)
class Submit:
    order_id: OrderId
    side: Side
    price: Price
    qty: Qty


@dataclass(frozen=True)
class Cancel:
    order_id: OrderId


Command = Submit | Cancel


class Pair:
    """The real book and the reference model, driven in lockstep."""

    def __init__(self) -> None:
        self.book = OrderBook()
        self.reference = ReferenceBook()
        self.audit = Audit()

    def apply(self, command: Command) -> list[Event]:
        if isinstance(command, Submit):
            args = (command.order_id, command.side, command.price, command.qty)
            actual = self.book.submit(*args)
            expected = self.reference.submit(*args)
        else:
            actual = self.book.cancel(command.order_id)
            expected = self.reference.cancel(command.order_id)

        assert actual == expected, f"{command}: book emitted {actual}, reference {expected}"
        self.audit.record(actual)
        self.book.check_invariants()
        snapshot = self.book.snapshot()
        assert snapshot == self.reference.snapshot(), f"{command}: snapshots diverged"
        audited = {
            order.order_id: (order.side, order.price, order.qty)
            for order in snapshot.bids + snapshot.asks
        }
        assert audited == self.audit.resting(), f"{command}: book disagrees with the audit"
        return actual
