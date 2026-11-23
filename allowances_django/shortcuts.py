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
from allowances.types import Allowance, FulfillmentResult, Transaction, Wallet, WalletSpec, WalletType
from allowances_django.permissions import PermissionProvider
from allowances_django.providers import SettingsProvider
from allowances_django.store import DjangoStore
from allowances_django.subjects import object_ref, resolve, subject_ref, subject_refs


def get_store(store: DjangoStore | None = None) -> DjangoStore:
    return store if store is not None else DjangoStore()


def get_provider(provider: DefinitionProvider | None = None) -> DefinitionProvider:
    """``provider``, or the default: ``settings.ALLOWANCES`` plus the ``perm:`` permission features."""
    return provider if provider is not None else PermissionProvider(SettingsProvider())


def _wallet_id(wallet):
    return getattr(wallet, "id", wallet)


def _actor(actor):
    return None if actor is None else subject_ref(actor)


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
    actor=None,
    provider: DefinitionProvider | None = None,
    at: datetime | None = None,
    store: DjangoStore | None = None,
) -> FulfillmentResult:
    """
    Consume the licence. Raises :class:`allowances.errors.InsufficientBalanceError` and rolls back
    if the request cannot be met in full, unless ``allow_partial=True``. ``actor`` is the model
    instance recorded on the entries; without it, the only subject in ``subjects`` is the actor.
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
        actor=_actor(actor),
    )


def attach(guid: uuid.UUID, obj, *, store: DjangoStore | None = None) -> None:
    """Record which object the fulfillment under ``guid`` paid for."""
    engine.attach(get_store(store), guid, object_ref(obj))


def unfulfill(guid: uuid.UUID, *, at: datetime | None = None, actor=None, store: DjangoStore | None = None) -> None:
    """
    Reverse everything recorded under ``guid``, as storno entries carrying ``actor`` (a model
    instance). Safe to call twice.
    """
    engine.unfulfill(get_store(store), guid, at, actor=_actor(actor))


def credit(
    wallet,
    amount,
    *,
    guid: uuid.UUID | None = None,
    obj=None,
    actor=None,
    at: datetime | None = None,
    store: DjangoStore | None = None,
) -> Transaction:
    """
    Add ``amount`` to a wallet as a ``GRANT`` ledger entry. ``wallet`` is a wallet, a wallet row or
    its id; ``obj`` is the model instance that paid for it, ``actor`` the one it was made for.
    """
    return engine.credit(
        get_store(store),
        _wallet_id(wallet),
        amount,
        guid=guid,
        obj=None if obj is None else object_ref(obj),
        at=at,
        actor=_actor(actor),
    )


def transfer(
    from_wallet,
    to_wallet,
    amount,
    *,
    guid: uuid.UUID | None = None,
    obj=None,
    actor=None,
    at: datetime | None = None,
    store: DjangoStore | None = None,
) -> FulfillmentResult:
    """
    Move ``amount`` from one wallet to another of the same currency, as a ``TRANSFER`` debit and
    credit under one fulfillment record. Wallets are wallets, wallet rows or ids; ``obj`` and
    ``actor`` are model instances. Raises :class:`allowances.errors.InsufficientBalanceError` when
    the source does not hold ``amount``.
    """
    return engine.transfer(
        get_store(store),
        _wallet_id(from_wallet),
        _wallet_id(to_wallet),
        amount,
        guid=guid,
        obj=None if obj is None else object_ref(obj),
        actor=_actor(actor),
        at=at,
    )


def close_wallet(wallet, *, at: datetime | None = None, store: DjangoStore | None = None) -> Wallet:
    """
    End a wallet's validity at ``at`` (default now) without a ledger entry. ``wallet`` is a wallet,
    a wallet row or its id.
    """
    return engine.close_wallet(get_store(store), _wallet_id(wallet), at=at)


def adjust_wallet(wallet, allotment, *, at: datetime | None = None, store: DjangoStore | None = None) -> Wallet:
    """Change the allotment of a wallet that has one. ``wallet`` is a wallet, a wallet row or its id."""
    return engine.adjust_wallet(get_store(store), _wallet_id(wallet), allotment, at=at)


def consumed(wallet, since: datetime, until: datetime, *, store: DjangoStore | None = None):
    """Net amount drawn from ``wallet`` in ``[since, until]`` — what a postpaid budget bills."""
    return engine.consumed(get_store(store), _wallet_id(wallet), since, until)


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


def cancel_scheduled_switch(subjects: dict, *, store: DjangoStore | None = None) -> int:
    """Cancel the not-yet-effective plan switch of these subjects. Returns how many had one."""
    return engine.cancel_scheduled_switch(get_store(store), subject_refs(subjects))


def archive_expired_wallets(at: datetime | None = None, *, store: DjangoStore | None = None) -> int:
    """Sweep wallets past their ``valid_until`` into the archive. Meant for a periodic job."""
    return engine.archive_expired_wallets(get_store(store), at)


def find_unattached_fulfillment_records(older_than: datetime | None = None, *, store: DjangoStore | None = None):
    """Records with a consumption entry that never got a domain object attached — candidates for recovery."""
    return engine.find_unattached_fulfillment_records(get_store(store), older_than)


def effective_allowances(
    subject,
    *,
    at: datetime | None = None,
    wallet_types: Sequence[WalletType] | None = None,
    currencies: Sequence[str] | None = None,
    store: DjangoStore | None = None,
) -> list[Allowance]:
    """
    Every wallet valid at ``at`` of ``subject`` (a model instance or subject reference) and of its
    groups, with holder, depth and effective balance; see :func:`allowances.engine.effective_allowances`.
    """
    return engine.effective_allowances(
        get_store(store), subject_ref(subject), at=at, wallet_types=wallet_types, currencies=currencies
    )


def add_member(member, group, *, store: DjangoStore | None = None) -> None:
    """
    Make ``member`` a member of ``group`` (model instances or subject references). Raises
    :class:`allowances.errors.MembershipCycleError` when ``group`` is ``member`` or one of its members.
    """
    engine.add_member(get_store(store), subject_ref(member), subject_ref(group))


def remove_member(member, group, *, store: DjangoStore | None = None) -> bool:
    """End a direct membership. Returns whether there was one."""
    return engine.remove_member(get_store(store), subject_ref(member), subject_ref(group))


def groups_of(obj, *, store: DjangoStore | None = None) -> list:
    """
    The groups ``obj`` belongs to, directly or through other groups, nearest first, as model
    instances. Groups whose reference does not resolve to an existing object are left out.
    """
    groups = (resolve(ref) for ref in engine.groups_of(get_store(store), subject_ref(obj)))
    return [group for group in groups if group is not None]
