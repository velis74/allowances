"""
Engine functions for Django applications, as coroutines.

Same functions as :mod:`allowances.engine`, with a default store (``settings.ALLOWANCES_STORE``, by default
:class:`allowances_django.store.DjangoStore`; see :mod:`allowances_django.config`) and definition provider
(``settings.ALLOWANCES`` plus the ``perm:`` permission features); subjects are model instances (or
``SubjectRef``)::

    from allowances_django.shortcuts import fulfill

    result = await fulfill("send_email", 1, {"user": request.user, "team": team})

``store=`` and ``provider=`` replace the defaults, e.g. for a second database or per-tenant
definitions. :mod:`allowances_django.sync.shortcuts` has the same functions for synchronous code.
"""

from __future__ import annotations

import uuid

from datetime import datetime
from typing import Sequence

from allowances import engine
from allowances.definitions import DefinitionProvider
from allowances.store import Store
from allowances.types import Allowance, FulfillmentResult, Transaction, Wallet, WalletSpec, WalletType
from allowances_django.config import default_store
from allowances_django.permissions import PermissionProvider
from allowances_django.providers import SettingsProvider
from allowances_django.subjects import object_ref, subject_ref, subject_refs


def get_store(store: Store | None = None) -> Store:
    """``store``, or a new store from ``settings.ALLOWANCES_STORE``."""
    return default_store(store)


def get_provider(provider: DefinitionProvider | None = None) -> DefinitionProvider:
    """``provider``, or the default: ``settings.ALLOWANCES`` plus the ``perm:`` permission features."""
    return provider if provider is not None else PermissionProvider(SettingsProvider())


def _wallet_id(wallet):
    return getattr(wallet, "id", wallet)


def _actor(actor):
    return None if actor is None else subject_ref(actor)


async def peek(
    feature: str,
    quantity,
    subjects: dict,
    *,
    provider: DefinitionProvider | None = None,
    at: datetime | None = None,
    store: Store | None = None,
) -> FulfillmentResult:
    """Return what ``fulfill`` would grant, without writing or reserving anything."""
    return await engine.peek(get_store(store), feature, quantity, subject_refs(subjects), get_provider(provider), at)


async def fulfill(
    feature: str,
    quantity,
    subjects: dict,
    *,
    guid: uuid.UUID | None = None,
    allow_partial: bool = False,
    actor=None,
    metadata: dict | None = None,
    provider: DefinitionProvider | None = None,
    at: datetime | None = None,
    store: Store | None = None,
) -> FulfillmentResult:
    """
    Consume the licence. Raises :class:`allowances.errors.InsufficientBalanceError` and rolls back
    if the request cannot be met in full, unless ``allow_partial=True``. ``actor`` (a model instance)
    is recorded on the entries; without it, the actor is the subject when ``subjects`` has a single
    entry, otherwise none. ``metadata`` (JSON-serialisable) is recorded on every consumption entry.
    """
    return await engine.fulfill(
        get_store(store),
        feature,
        quantity,
        subject_refs(subjects),
        get_provider(provider),
        guid=guid,
        allow_partial=allow_partial,
        at=at,
        actor=_actor(actor),
        metadata=metadata,
    )


async def attach(guid: uuid.UUID, obj, *, store: Store | None = None) -> None:
    """Record which object the fulfilment under ``guid`` paid for."""
    await engine.attach(get_store(store), guid, object_ref(obj))


async def transactions_for_object(obj, *, store: Store | None = None) -> list[Transaction]:
    """
    The ledger entries of ``obj`` (a model instance or ``ObjectRef``), oldest first: the entries attached to it
    and the stornos that reverse them; see :func:`allowances.engine.transactions_for_object`.
    """
    return await engine.transactions_for_object(get_store(store), object_ref(obj))


async def unfulfill(guid: uuid.UUID, *, at: datetime | None = None, actor=None, store: Store | None = None) -> None:
    """
    Reverse everything recorded under ``guid``, as storno entries carrying ``actor`` (a model
    instance) and the metadata of the entries they reverse. Safe to call twice.
    """
    await engine.unfulfill(get_store(store), guid, at, actor=_actor(actor))


async def credit(
    wallet,
    amount,
    *,
    guid: uuid.UUID | None = None,
    obj=None,
    actor=None,
    metadata: dict | None = None,
    at: datetime | None = None,
    store: Store | None = None,
) -> Transaction:
    """
    Add ``amount`` to a wallet as a ``GRANT`` ledger entry. ``wallet`` is a wallet, a wallet row or
    its id; ``obj`` is the model instance that paid for it, ``actor`` the model instance on whose
    behalf it was made; ``metadata`` (JSON-serialisable) is recorded on the entry.
    """
    return await engine.credit(
        get_store(store),
        _wallet_id(wallet),
        amount,
        guid=guid,
        obj=None if obj is None else object_ref(obj),
        at=at,
        actor=_actor(actor),
        metadata=metadata,
    )


async def transfer(
    from_wallet,
    to_wallet,
    amount,
    *,
    guid: uuid.UUID | None = None,
    obj=None,
    actor=None,
    metadata: dict | None = None,
    at: datetime | None = None,
    store: Store | None = None,
) -> FulfillmentResult:
    """
    Move ``amount`` from one wallet to another of the same currency, as a ``TRANSFER`` debit and
    credit under one fulfilment record. Wallets are wallets, wallet rows or ids; ``obj`` and
    ``actor`` are model instances; ``metadata`` (JSON-serialisable) is recorded on both entries.
    Raises :class:`allowances.errors.InsufficientBalanceError` when the source does not hold ``amount``.
    """
    return await engine.transfer(
        get_store(store),
        _wallet_id(from_wallet),
        _wallet_id(to_wallet),
        amount,
        guid=guid,
        obj=None if obj is None else object_ref(obj),
        actor=_actor(actor),
        metadata=metadata,
        at=at,
    )


async def close_wallet(wallet, *, at: datetime | None = None, store: Store | None = None) -> Wallet:
    """
    End a wallet's validity at ``at`` (default now) without a ledger entry. ``wallet`` is a wallet,
    a wallet row or its id.
    """
    return await engine.close_wallet(get_store(store), _wallet_id(wallet), at=at)


async def adjust_wallet(wallet, allotment, *, at: datetime | None = None, store: Store | None = None) -> Wallet:
    """Change the allotment of a wallet that has one. ``wallet`` is a wallet, a wallet row or its id."""
    return await engine.adjust_wallet(get_store(store), _wallet_id(wallet), allotment, at=at)


async def consumed(wallet, since: datetime, until: datetime, *, store: Store | None = None):
    """Net amount drawn from ``wallet`` in ``[since, until]``."""
    return await engine.consumed(get_store(store), _wallet_id(wallet), since, until)


async def open_wallet(
    subject,
    spec: WalletSpec,
    *,
    source: str,
    effective_date: datetime | None = None,
    store: Store | None = None,
) -> Wallet:
    """Open one wallet for ``subject``. A ``source`` other than ``"plan"`` keeps it out of plan switches."""
    return await engine.open_wallet(
        get_store(store), subject_ref(subject), spec, source=source, effective_date=effective_date
    )


async def plan_switch(
    subjects: dict,
    open_wallets: Sequence[WalletSpec],
    effective_date: datetime | None = None,
    *,
    store: Store | None = None,
) -> list[Wallet]:
    """
    Move these subjects onto the plan described by ``open_wallets``, atomically. Only wallets with
    source ``"plan"`` are closed or adjusted; see :func:`allowances.engine.plan_switch`.
    """
    return await engine.plan_switch(get_store(store), subject_refs(subjects), open_wallets, effective_date)


async def cancel_scheduled_switch(subjects: dict, *, store: Store | None = None) -> int:
    """Cancel the not-yet-effective plan switch of these subjects. Returns how many had one."""
    return await engine.cancel_scheduled_switch(get_store(store), subject_refs(subjects))


async def archive_expired_wallets(at: datetime | None = None, *, store: Store | None = None) -> int:
    """Archive wallets whose ``valid_until`` is at or before ``at``. Returns how many were archived."""
    return await engine.archive_expired_wallets(get_store(store), at)


async def find_unattached_fulfillment_records(older_than: datetime | None = None, *, store: Store | None = None):
    """Fulfilment records with a consumption entry that has no domain object attached."""
    return await engine.find_unattached_fulfillment_records(get_store(store), older_than)


async def effective_allowances(
    subject,
    *,
    at: datetime | None = None,
    wallet_types: Sequence[WalletType] | None = None,
    currencies: Sequence[str] | None = None,
    store: Store | None = None,
) -> list[Allowance]:
    """
    Every wallet valid at ``at`` of ``subject`` (a model instance or subject reference) and of its
    groups, with holder, depth and effective balance; see :func:`allowances.engine.effective_allowances`.
    """
    return await engine.effective_allowances(
        get_store(store), subject_ref(subject), at=at, wallet_types=wallet_types, currencies=currencies
    )


async def add_member(member, group, *, store: Store | None = None) -> None:
    """
    Make ``member`` a member of ``group`` (model instances or subject references). Raises
    :class:`allowances.errors.MembershipCycleError` when ``group`` is ``member`` or one of its members.
    """
    await engine.add_member(get_store(store), subject_ref(member), subject_ref(group))


async def remove_member(member, group, *, store: Store | None = None) -> bool:
    """End a direct membership. Returns whether there was one."""
    return await engine.remove_member(get_store(store), subject_ref(member), subject_ref(group))


async def groups_of(obj, *, store: Store | None = None) -> list:
    """
    The groups ``obj`` belongs to, directly or through other groups, nearest first, as model
    instances. Groups whose reference does not resolve to an existing object are left out.
    """
    store = get_store(store)
    groups = [await store.resolve(ref) for ref in await engine.groups_of(store, subject_ref(obj))]
    return [group for group in groups if group is not None]
