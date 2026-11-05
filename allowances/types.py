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
    """
    A grant (balance added by :func:`allowances.engine.credit` or changed by a plan adjustment), a
    consumption (balance drawn), or a storno (an earlier entry reversed).
    """

    GRANT = "grant"
    CONSUMPTION = "consumption"
    STORNO = "storno"


class SwitchPolicy(StrEnum):
    """
    What :func:`allowances.engine.plan_switch` does with a subject's current plan wallet of the same
    type and currency as a :class:`WalletSpec`.

    ``REPLACE`` closes it and opens a new wallet. ``ADJUST`` keeps it and adds the difference between
    the new and the old allotment to its balance, which may be negative. ``TOP_UP`` keeps it and adds
    only a positive difference, and its allotment never decreases, so a downgrade followed by an
    upgrade to the original plan adds nothing. On a windowed wallet both ``ADJUST`` and ``TOP_UP`` set
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

    ``source`` names where the wallet came from. :func:`allowances.engine.plan_switch` only opens and
    closes wallets with ``source == "plan"``; any other value (``"purchase"``, ``"budget"``) is left
    alone by it. ``allotment`` is the plan's amount for a wallet opened with an ``ADJUST`` or
    ``TOP_UP`` policy, the base the next plan switch computes its difference against. It is ``None``
    for every other wallet.
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
