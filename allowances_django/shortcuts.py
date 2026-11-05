"""
The API a Django application actually calls.

Same functions as :mod:`allowances.engine`, minus the ceremony: the store and the definition
provider are supplied for you, and subjects are the model instances you already have rather than
references you have to construct.

    from allowances_django.shortcuts import fulfill

    result = fulfill("send_email", 1, {"user": request.user, "team": team})

Pass ``store=`` or ``provider=`` to override either — a second database, or per-tenant definitions
assembled at request time.
"""

from __future__ import annotations

import uuid

from datetime import datetime
from typing import Sequence

from allowances import engine
from allowances.definitions import DefinitionProvider
from allowances.types import FulfillmentResult, Transaction, Wallet, WalletSpec
from allowances_django.providers import SettingsProvider
from allowances_django.store import DjangoStore
from allowances_django.subjects import object_ref, subject_ref, subject_refs


def get_store(store: DjangoStore | None = None) -> DjangoStore:
    return store if store is not None else DjangoStore()


def get_provider(provider: DefinitionProvider | None = None) -> DefinitionProvider:
    return provider if provider is not None else SettingsProvider()


def peek(
    feature: str,
    quantity,
    subjects: dict,
    *,
    provider: DefinitionProvider | None = None,
    at: datetime | None = None,
    store: DjangoStore | None = None,
) -> FulfillmentResult:
    """Would this request succeed? Writes nothing, reserves nothing."""
    return engine.peek(get_store(store), feature, quantity, subject_refs(subjects), get_provider(provider), at)


def fulfill(
    feature: str,
    quantity,
    subjects: dict,
    *,
    guid: uuid.UUID | None = None,
    allow_partial: bool = False,
    provider: DefinitionProvider | None = None,
    at: datetime | None = None,
    store: DjangoStore | None = None,
) -> FulfillmentResult:
    """
    Consume the licence. Raises :class:`allowances.errors.InsufficientBalanceError` and rolls back
    if the request cannot be met in full, unless ``allow_partial=True``.
    """
    return engine.fulfill(
        get_store(store),
        feature,
        quantity,
        subject_refs(subjects),
        get_provider(provider),
        guid=guid,
        allow_partial=allow_partial,
        at=at,
    )


def attach(guid: uuid.UUID, obj, *, store: DjangoStore | None = None) -> None:
    """Record which object the fulfillment under ``guid`` paid for."""
    engine.attach(get_store(store), guid, object_ref(obj))


def unfulfill(guid: uuid.UUID, *, at: datetime | None = None, store: DjangoStore | None = None) -> None:
    """Reverse everything consumed under ``guid``, as storno entries. Safe to call twice."""
    engine.unfulfill(get_store(store), guid, at)


def credit(
    wallet,
    amount,
    *,
    guid: uuid.UUID | None = None,
    obj=None,
    at: datetime | None = None,
    store: DjangoStore | None = None,
) -> Transaction:
    """
    Add ``amount`` to a wallet as a ``GRANT`` ledger entry. ``wallet`` is a wallet, a wallet row or
    its id; ``obj`` is the model instance that paid for it.
    """
    return engine.credit(
        get_store(store),
        getattr(wallet, "id", wallet),
        amount,
        guid=guid,
        obj=None if obj is None else object_ref(obj),
        at=at,
    )


def adjust_wallet(wallet, allotment, *, at: datetime | None = None, store: DjangoStore | None = None) -> Wallet:
    """Change the allotment of a wallet that has one. ``wallet`` is a wallet, a wallet row or its id."""
    return engine.adjust_wallet(get_store(store), getattr(wallet, "id", wallet), allotment, at=at)


def consumed(wallet, since: datetime, until: datetime, *, store: DjangoStore | None = None):
    """Net amount drawn from ``wallet`` in ``[since, until]`` — what a postpaid budget bills."""
    return engine.consumed(get_store(store), getattr(wallet, "id", wallet), since, until)


def open_wallet(
    subject,
    spec: WalletSpec,
    *,
    source: str,
    effective_date: datetime | None = None,
    store: DjangoStore | None = None,
) -> Wallet:
    """Open one wallet for ``subject``. A ``source`` other than ``"plan"`` keeps it out of plan switches."""
    return engine.open_wallet(
        get_store(store), subject_ref(subject), spec, source=source, effective_date=effective_date
    )


def plan_switch(
    subjects: dict,
    open_wallets: Sequence[WalletSpec],
    effective_date: datetime | None = None,
    *,
    store: DjangoStore | None = None,
) -> list[Wallet]:
    """
    Move these subjects onto the plan described by ``open_wallets``, atomically. Only wallets with
    source ``"plan"`` are closed or adjusted; see :func:`allowances.engine.plan_switch`.
    """
    return engine.plan_switch(get_store(store), subject_refs(subjects), open_wallets, effective_date)


def archive_expired_wallets(at: datetime | None = None, *, store: DjangoStore | None = None) -> int:
    """Sweep wallets past their ``valid_until`` into the archive. Meant for a periodic job."""
    return engine.archive_expired_wallets(get_store(store), at)


def find_unattached_fulfillment_records(older_than: datetime | None = None, *, store: DjangoStore | None = None):
    """Records whose transactions never got a domain object attached — candidates for recovery."""
    return engine.find_unattached_fulfillment_records(get_store(store), older_than)
