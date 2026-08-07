"""
The value types the core passes around.

Every type here is a plain, frozen dataclass — no ORM, no framework. A storage backend is free
to persist these however it likes (relational rows, Redis hashes, a dict); what it hands back to
the engine is always one of these.

The one type that is *not* here is the archive representation: what an archived wallet looks like
is entirely a backend's business (see :meth:`allowances.store.Store.archive_wallet`), so the core
never names it.
"""

from __future__ import annotations

import uuid

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class WalletType(StrEnum):
    """
    Which section of a Feature definition a wallet fulfils.

    A feature may declare a ``grant`` section, a ``consumable`` section, or both, and a
    ``fulfillment_order`` walks wallets by ``(type, subject)`` pairs — so a wallet has to record
    which of the two it belongs to.
    """

    GRANT = "grant"
    CONSUMABLE = "consumable"


class TransactionKind(StrEnum):
    """A grant (balance added), a consumption (balance drawn), or a storno (an earlier entry reversed)."""

    GRANT = "grant"
    CONSUMPTION = "consumption"
    STORNO = "storno"


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """
    A reference to whatever holds a licence — a user, an organisation, a device, a location.

    The core never sees the subject object itself, only this pair. ``type`` is an opaque string
    the host application chooses and a backend maps back to a real object; the Django adapter uses
    ``"app_label.modelname"``. ``id`` is always stored as a string so integer, UUID and slug
    primary keys all work without the core caring which.
    """

    type: str
    id: str

    def __init__(self, type: str, id: object) -> None:
        object.__setattr__(self, "type", type)
        object.__setattr__(self, "id", str(id))

    def __str__(self) -> str:
        return f"{self.type}:{self.id}"

    @classmethod
    def parse(cls, value: str) -> SubjectRef:
        """Inverse of ``__str__``: ``SubjectRef.parse("auth.user:42")``."""
        type_, _, id_ = value.partition(":")
        if not type_ or not id_:
            raise ValueError(f"Not a subject reference: {value!r} (expected 'type:id')")
        return cls(type_, id_)


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """
    A reference to the domain object a fulfillment ended up paying for — the email that was sent,
    the row that was created. Attached after the fact by :func:`allowances.engine.attach`.
    """

    type: str
    id: str

    def __init__(self, type: str, id: object) -> None:
        object.__setattr__(self, "type", type)
        object.__setattr__(self, "id", str(id))

    def __str__(self) -> str:
        return f"{self.type}:{self.id}"


@dataclass(frozen=True, slots=True)
class Wallet:
    """
    A subject's holding of one currency.

    ``balance`` means different things depending on ``reset_config``. Without one it is the
    authoritative stored balance, drawn down by every consumption. With one it is the *per-window
    allotment* — a constant that fulfillment never mutates — and the balance that actually governs
    decisions is computed on the fly by summing transactions inside the current window. See
    :func:`allowances.engine.effective_balance`.
    """

    id: object
    subject: SubjectRef
    wallet_type: WalletType
    currency: str
    balance: Decimal
    created_at: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    overdraft_limit: Decimal | None = None
    first_overdraft_at: datetime | None = None
    reset_config: dict | None = None

    def is_valid_at(self, at: datetime) -> bool:
        if self.valid_from is not None and self.valid_from > at:
            return False
        if self.valid_until is not None and self.valid_until < at:
            return False
        return True


@dataclass(frozen=True, slots=True)
class Transaction:
    """
    One ledger entry against a wallet. ``amount`` is signed: negative for consumption, positive for
    a grant or for the storno that reverses a consumption.
    """

    id: object
    wallet_id: object
    kind: TransactionKind
    amount: Decimal
    created_at: datetime
    record_guid: uuid.UUID | None = None
    feature: str = ""
    storno_of_id: object = None
    obj: ObjectRef | None = None


@dataclass(frozen=True, slots=True)
class FulfillmentRecord:
    """Groups every transaction belonging to one logical business operation, under one ``guid``."""

    guid: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Modifier:
    """A time-limited, subject-scoped factor adjusting what one unit of a feature costs."""

    id: object
    subject: SubjectRef
    feature: str
    factor: Decimal
    valid_from: datetime | None = None
    valid_until: datetime | None = None


@dataclass
class WalletSpec:
    """
    Describes a wallet to open, without saying for whom — :func:`allowances.engine.plan_switch`
    opens one wallet per ``(subject, spec)`` pair across every subject it is given.
    """

    wallet_type: WalletType
    currency: str
    balance: Decimal = Decimal("0")
    overdraft_limit: Decimal | None = None
    reset_config: dict | None = None
    valid_until: datetime | None = None


@dataclass
class FulfillmentResult:
    """What both :func:`allowances.engine.peek` and :func:`allowances.engine.fulfill` return."""

    requested: Decimal
    granted: Decimal
    satisfied: bool
    transactions: list[Transaction]
    guid: uuid.UUID | None
    modifier_applied: Decimal | None = None
