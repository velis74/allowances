"""
The value types the core passes around.

Every type here is a plain dataclass with no ORM or framework dependency. A backend may persist
them in any form (relational rows, Redis hashes, a dict); it returns these types to the engine.

Archived wallets have no core type; their representation is backend-specific (see
:meth:`allowances.store.Store.archive_wallet`).
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
    """
    A grant (balance added by :func:`allowances.engine.credit` or changed by a plan adjustment), a
    consumption (balance drawn; zero for a use of a rate-0 right), a transfer (one leg of
    :func:`allowances.engine.transfer`), or a storno (an earlier entry reversed).
    """

    GRANT = "grant"
    CONSUMPTION = "consumption"
    TRANSFER = "transfer"
    STORNO = "storno"


class SwitchPolicy(StrEnum):
    """
    What :func:`allowances.engine.plan_switch` does with a subject's current plan wallet of the same
    type and currency as a :class:`WalletSpec`.

    ``REPLACE`` closes it and opens a new wallet. ``ADJUST`` keeps it and adds the difference between
    the new and the old allotment to its balance, which may be negative. ``TOP_UP`` keeps it and adds
    only a positive difference; both record the new allotment. On a windowed wallet both set
    the per-window allotment to the new value.

    A kept wallet has no ``allotment`` when it was opened under ``REPLACE``; it is then replaced. A
    kept wallet whose ``reset_config`` is set while the spec's is not, or the other way round, is
    replaced too.
    """

    REPLACE = "replace"
    ADJUST = "adjust"
    TOP_UP = "top_up"


#: ``Wallet.source`` of the wallets :func:`allowances.engine.plan_switch` opens and closes.
PLAN_SOURCE = "plan"


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """
    A reference to whatever holds a licence — a user, an organisation, a device, a location.

    The core never sees the subject object itself, only this pair. ``type`` is an opaque string
    the host application chooses and a backend maps back to a real object; the Django adapter uses
    ``"app_label.modelname"``. ``id`` is stored as a string, so integer, UUID and slug
    primary keys are handled alike.
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
    A reference to the domain object a fulfilment paid for (a sent email, a created row). Set by
    :func:`allowances.engine.attach`.
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
    authoritative stored balance, drawn down by every consumption. With one it is the per-window
    allotment, which fulfilment does not change; the effective balance is the allotment plus the
    sum of transactions inside the current window. See :func:`allowances.engine.effective_balance`.

    ``source`` names the origin of the wallet. :func:`allowances.engine.plan_switch` only opens and
    closes wallets with ``source == "plan"``; wallets with any other source (``"purchase"``,
    ``"budget"``) are not changed by it. ``allotment`` is the plan's amount for a wallet opened with
    an ``ADJUST`` or ``TOP_UP`` policy, the base the next plan switch computes its difference against.
    It is ``None`` for every other wallet. ``scheduled`` is set while a plan switch with a future effective date
    affects the wallet; it records what that switch changes, so the change applies on its date and
    can be cancelled before it. Its content is engine-internal; a backend stores it unchanged.
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
    source: str = PLAN_SOURCE
    allotment: Decimal | None = None
    scheduled: dict | None = None

    def is_valid_at(self, at: datetime) -> bool:
        if self.valid_from is not None and self.valid_from > at:
            return False
        if self.valid_until is not None and self.valid_until <= at:
            return False
        return True


@dataclass(frozen=True, slots=True)
class Allowance:
    """
    One live wallet a subject holds, directly or through a group, as returned by
    :func:`allowances.engine.effective_allowances`.

    ``wallet`` is the wallet as it is at the queried moment (a scheduled adjustment that has come
    due applied, unsaved). ``holder`` owns the wallet: the queried subject or one of its groups.
    ``depth`` is 0 for the subject itself, 1 for its direct groups, 2 for their groups, and so on.
    ``balance`` is the wallet's :func:`~allowances.engine.effective_balance` at that moment.
    """

    wallet: Wallet
    holder: SubjectRef
    depth: int
    balance: Decimal


@dataclass(frozen=True, slots=True)
class Transaction:
    """
    One ledger entry against a wallet. ``amount`` is signed: negative for consumption and for the
    debit leg of a transfer, positive for a grant and for the credit leg of a transfer; a storno
    carries the negated amount of the entry it reverses.

    ``actor`` is the subject on whose behalf the entry was made, or ``None`` when none was given.

    ``metadata`` is the application's data recorded with the entry (JSON-serialisable), or ``None``.
    The library does not read it.
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
    actor: SubjectRef | None = None
    metadata: dict | None = None


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

    ``on_switch`` decides what a later plan switch does with the subject's existing plan wallet of
    the same type and currency; see :class:`SwitchPolicy`.
    """

    wallet_type: WalletType
    currency: str
    balance: Decimal = Decimal("0")
    overdraft_limit: Decimal | None = None
    reset_config: dict | None = None
    valid_until: datetime | None = None
    on_switch: SwitchPolicy = SwitchPolicy.REPLACE


@dataclass
class FulfillmentResult:
    """What both :func:`allowances.engine.peek` and :func:`allowances.engine.fulfill` return."""

    requested: Decimal
    granted: Decimal
    satisfied: bool
    transactions: list[Transaction]
    guid: uuid.UUID | None
    modifier_applied: Decimal | None = None
