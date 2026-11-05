"""
The fulfillment engine — ``peek``, ``fulfill``, ``attach``, ``unfulfill``, ``credit``,
``open_wallet``, ``adjust_wallet``, ``consumed``, ``plan_switch``.

Nothing in this module knows what a database is. It receives a :class:`~allowances.store.Store`
and a :class:`~allowances.definitions.DefinitionProvider` and does arithmetic between them, which
is why the same code runs against Postgres, an in-memory dict, or anything else somebody
implements the port for.

Every function takes ``store`` first. The Django adapter wraps these in module-level shortcuts
that supply a default store, so application code rarely passes one explicitly.
"""

from __future__ import annotations

import uuid

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Sequence

from allowances.definitions import DefinitionProvider, FeatureDefinition
from allowances.errors import (
    InsufficientBalanceError,
    RecordNotFoundError,
    UnsuitableWalletError,
    WalletNotFoundError,
)
from allowances.rounding import apply_round
from allowances.store import Store
from allowances.types import (
    FulfillmentResult,
    ObjectRef,
    PLAN_SOURCE,
    SubjectRef,
    SwitchPolicy,
    Transaction,
    TransactionKind,
    Wallet,
    WalletSpec,
    WalletType,
)
from allowances.windows import utcnow, window_start


@dataclass
class _Draw:
    """One planned withdrawal: take ``cost`` units of currency out of ``wallet`` to cover ``qty``
    units of the feature being requested."""

    wallet: Wallet
    cost: Decimal
    qty: Decimal


# -------------------------------------------------------------------------------------------------
# Balances
# -------------------------------------------------------------------------------------------------


def effective_balance(store: Store, wallet: Wallet, at: datetime | None = None) -> Decimal:
    """
    The balance that governs a decision right now.

    Without a ``reset_config`` the stored balance is authoritative. With one, the stored balance is
    the per-window allotment and the transactions inside the current window are added to it — they
    are negative for consumption, so the balance nets down as the window fills and climbs back as
    old entries age out.
    """
    at = at or utcnow()
    if not wallet.reset_config:
        return wallet.balance
    since = window_start(wallet.reset_config, at)
    return wallet.balance + store.sum_transactions(wallet_id=wallet.id, since=since, until=at)


# -------------------------------------------------------------------------------------------------
# Planning
# -------------------------------------------------------------------------------------------------


def _ordered_wallets(
    store: Store, feature_def: FeatureDefinition, subjects: dict, at: datetime, lock: bool
) -> list[Wallet]:
    """
    Expand ``fulfillment_order`` into a concrete, deduplicated list of candidate wallets in the
    order they should be drawn from.

    A callable ``fulfillment_order`` gets one flat list of every plausible candidate and decides
    the order itself. A list of steps is walked in declaration order; within a step, wallets that
    expire soonest come first, wallets without ``valid_until`` last, and equal expiries oldest-first.
    """
    subject_refs = list(subjects.values())
    if not subject_refs:
        return []

    if callable(feature_def.fulfillment_order):
        wallet_types = [wt for wt in (WalletType.GRANT, WalletType.CONSUMABLE) if feature_def.section_for(wt)]
        if not wallet_types:
            return []
        currencies: set[str] = set()
        for wallet_type in wallet_types:
            currencies.update(feature_def.section_for(wallet_type).currency_names)
        candidates = store.find_wallets(
            subjects=subject_refs,
            wallet_types=wallet_types,
            currencies=sorted(currencies),
            at=at,
            lock=lock,
        )
        return list(feature_def.fulfillment_order(candidates))

    seen: set = set()
    ordered: list[Wallet] = []
    for step in feature_def.fulfillment_order:
        step_type, step_subject, step_source = (*step, None)[:3]
        wallet_types = [step_type] if step_type is not None else [WalletType.GRANT, WalletType.CONSUMABLE]
        # A step naming a subject the caller didn't supply is skipped, not an error — features are
        # declared once and called from contexts that legitimately know about different subjects.
        if step_subject is None:
            step_subjects = subject_refs
        elif step_subject in subjects:
            step_subjects = [subjects[step_subject]]
        else:
            step_subjects = []
        for wallet_type in wallet_types:
            section = feature_def.section_for(wallet_type)
            if section is None or not step_subjects:
                continue
            for subject in step_subjects:
                found = store.find_wallets(
                    subjects=[subject],
                    wallet_types=[wallet_type],
                    currencies=section.currency_names,
                    at=at,
                    lock=lock,
                )
                for wallet in sorted(found, key=_expiry_key):
                    if step_source is not None and wallet.source != step_source:
                        continue
                    if wallet.id not in seen:
                        seen.add(wallet.id)
                        ordered.append(wallet)
    return ordered


def _expiry_key(wallet: Wallet):
    """Soonest ``valid_until`` first, wallets that never expire last. ``sorted`` keeps store order for ties."""
    return (wallet.valid_until is None, wallet.valid_until or datetime.min)


def _consume(
    store: Store,
    wallets: Iterable[Wallet],
    feature_def: FeatureDefinition,
    remaining: Decimal,
    modifier_factor: Decimal,
    provider: DefinitionProvider,
    at: datetime,
    use_overdraft: bool,
    pending: dict,
) -> tuple[list[_Draw], Decimal]:
    """
    Walk the candidates once, planning draws until the request is covered. Nothing is persisted
    here.

    ``pending`` carries in-flight draws per wallet id across calls, so the overdraft pass sees what
    the ordinary pass already took and doesn't spend the same balance twice.
    """
    drawn: list[_Draw] = []
    for wallet in wallets:
        if remaining <= 0:
            break
        section = feature_def.section_for(wallet.wallet_type)
        rate = section.rate_for(wallet.currency) if section else None
        if rate is None:
            continue

        already = pending.get(wallet.id, Decimal("0"))
        balance = effective_balance(store, wallet, at) - already

        if use_overdraft:
            # Only wallets that were given an explicit overdraft limit take part in the second pass.
            if wallet.overdraft_limit is None:
                continue
            capacity = max(balance, Decimal("0")) + wallet.overdraft_limit
        else:
            capacity = max(balance, Decimal("0"))
        if capacity <= 0:
            continue

        effective_rate = rate * modifier_factor
        if effective_rate == 0:
            # A zero rate means the feature is free out of this wallet — it covers the whole
            # remainder without costing anything.
            qty_from_wallet = remaining
        else:
            qty_from_wallet = min(remaining, capacity / effective_rate)
        if qty_from_wallet <= 0:
            continue

        cost = apply_round(qty_from_wallet * effective_rate, provider.get_currency(wallet.currency).round_fn)
        if cost <= 0:
            continue
        if cost > capacity:
            # Rounding pushed the cost past what is actually there. Clamp rather than overdraw:
            # never spend more than the wallet holds, even at the price of covering slightly less
            # than the rounding implied.
            cost = capacity

        qty_covered = remaining if effective_rate == 0 else min(cost / effective_rate, remaining)

        drawn.append(_Draw(wallet=wallet, cost=cost, qty=qty_covered))
        pending[wallet.id] = already + cost
        remaining -= qty_covered

    return drawn, remaining


def _modifier_factor(store: Store, feature: str, subjects: dict, at: datetime) -> Decimal:
    """Where several modifiers apply, the subject gets the most favourable one."""
    if not subjects:
        return Decimal("1")
    factors = store.find_modifier_factors(feature=feature, subjects=list(subjects.values()), at=at)
    return max(factors) if factors else Decimal("1")


def _plan(
    store: Store,
    feature_def: FeatureDefinition,
    feature: str,
    quantity: Decimal,
    subjects: dict,
    provider: DefinitionProvider,
    at: datetime,
    lock: bool,
) -> tuple[list[_Draw], Decimal, Decimal]:
    """
    Plan the whole request: ordinary balances first, and only if something is still uncovered and
    the feature permits it, a second pass over wallets with overdraft headroom.
    """
    pending: dict = {}
    candidates = _ordered_wallets(store, feature_def, subjects, at, lock)
    factor = _modifier_factor(store, feature, subjects, at)

    drawn, remaining = _consume(store, candidates, feature_def, quantity, factor, provider, at, False, pending)
    if remaining > 0 and feature_def.overdraft:
        more, remaining = _consume(store, candidates, feature_def, remaining, factor, provider, at, True, pending)
        drawn = drawn + more
    return drawn, remaining, factor


# -------------------------------------------------------------------------------------------------
# Archival
# -------------------------------------------------------------------------------------------------


def _archive_if_drained(store: Store, wallet: Wallet) -> bool:
    """
    Archive a wallet the moment it is spent out, so the live tables only ever hold wallets that can
    still pay for something.

    A windowed wallet is never archived by this rule — it replenishes every window by design. A
    wallet with an overdraft limit isn't either, since a zero balance still leaves it able to fund
    the next request. Nor is a wallet with an ``allotment``: the next plan switch adjusts it.
    """
    if wallet.reset_config or wallet.allotment is not None:
        return False
    if wallet.balance <= 0 and wallet.overdraft_limit is None:
        store.archive_wallet(wallet)
        return True
    return False


def archive_expired_wallets(store: Store, at: datetime | None = None) -> int:
    """
    Archive wallets whose ``valid_until`` has passed. Returns how many were archived.

    Expired wallets are already invisible to fulfillment, so nothing will ever draw from them again
    and the drain-triggered archival above will never see them. A host application is expected to
    call this periodically — a Celery beat task, a cron job, a management command.
    """
    at = at or utcnow()
    archived = 0
    with store.unit_of_work():
        for wallet in store.find_expired_wallets(at=at, lock=True):
            store.archive_wallet(wallet)
            archived += 1
    return archived


# -------------------------------------------------------------------------------------------------
# Core API
# -------------------------------------------------------------------------------------------------


def peek(
    store: Store,
    feature: str,
    quantity,
    subjects: dict,
    provider: DefinitionProvider,
    at: datetime | None = None,
) -> FulfillmentResult:
    """
    Ask whether a request *could* be met, without touching anything.

    No transactions, no fulfillment record, no balance changes. The answer is a snapshot and
    carries no reservation — a concurrent caller can spend what this said was available.
    """
    feature_def = provider.get_feature(feature)
    at = at or utcnow()
    quantity = Decimal(str(quantity))
    subjects = subjects or {}

    _, remaining, factor = _plan(store, feature_def, feature, quantity, subjects, provider, at, lock=False)
    return FulfillmentResult(
        requested=quantity,
        granted=quantity - remaining,
        satisfied=remaining <= 0,
        transactions=[],
        guid=None,
        modifier_applied=factor if factor != Decimal("1") else None,
    )


def fulfill(
    store: Store,
    feature: str,
    quantity,
    subjects: dict,
    provider: DefinitionProvider,
    guid: uuid.UUID | None = None,
    allow_partial: bool = False,
    at: datetime | None = None,
) -> FulfillmentResult:
    """
    Actually consume the licence, writing one transaction per wallet drawn from.

    With ``allow_partial=False`` (the default) a request that cannot be met in full raises
    :class:`~allowances.errors.InsufficientBalanceError` and the whole call is rolled back — the
    caller gets all of it or none of it. With ``allow_partial=True`` whatever was available is
    consumed and ``result.satisfied`` reports whether that was enough.

    Passing a ``guid`` groups this call with earlier ones under a single fulfillment record, so one
    business operation spanning several features can be reversed as a unit.
    """
    feature_def = provider.get_feature(feature)
    at = at or utcnow()
    quantity = Decimal(str(quantity))
    subjects = subjects or {}

    with store.unit_of_work():
        record = store.get_or_create_record(guid)
        drawn, remaining, factor = _plan(store, feature_def, feature, quantity, subjects, provider, at, lock=True)
        satisfied = remaining <= 0
        granted = quantity - remaining

        if not satisfied and not allow_partial:
            raise InsufficientBalanceError(
                FulfillmentResult(
                    requested=quantity,
                    granted=granted,
                    satisfied=False,
                    transactions=[],
                    guid=record.guid,
                    modifier_applied=None,
                )
            )

        created: list[Transaction] = []
        # A wallet can be drawn from twice — once on the ordinary pass, once on the overdraft pass.
        # Track its running state here so the second draw starts from the first draw's result.
        current: dict = {}
        for draw in drawn:
            wallet = current.get(draw.wallet.id, draw.wallet)
            created.append(
                store.create_transaction(
                    wallet_id=wallet.id,
                    kind=TransactionKind.CONSUMPTION,
                    amount=-draw.cost,
                    record_guid=record.guid,
                    feature=feature,
                    at=at,
                )
            )

            if not wallet.reset_config:
                balance = wallet.balance - draw.cost
                first_overdraft = wallet.first_overdraft_at
                if balance < 0 and first_overdraft is None:
                    first_overdraft = at
                wallet = replace(wallet, balance=balance, first_overdraft_at=first_overdraft)
                store.save_wallet(wallet)
            elif wallet.first_overdraft_at is None and effective_balance(store, wallet, at) < 0:
                # A windowed wallet's stored balance never moves, but it can still go negative
                # within a window, and that first moment is worth recording.
                wallet = replace(wallet, first_overdraft_at=at)
                store.save_wallet(wallet)

            current[wallet.id] = wallet
            _archive_if_drained(store, wallet)

        return FulfillmentResult(
            requested=quantity,
            granted=granted,
            satisfied=satisfied,
            transactions=created,
            guid=record.guid,
            modifier_applied=factor if factor != Decimal("1") else None,
        )


def attach(store: Store, guid: uuid.UUID, obj: ObjectRef) -> None:
    """
    Record which domain object a fulfillment paid for.

    Fulfillment happens before the business operation completes — you consume the quota, then send
    the email. This closes the loop afterwards, and it is what makes a record with unattached
    transactions a usable signal that something died in between.
    """
    with store.unit_of_work():
        if store.get_record(guid) is None:
            raise RecordNotFoundError(f"No fulfillment record with guid {guid}")
        pending = [t.id for t in store.transactions_for_record(record_guid=guid) if t.obj is None]
        if pending:
            store.attach_object(transaction_ids=pending, obj=obj)


def unfulfill(store: Store, guid: uuid.UUID, at: datetime | None = None) -> None:
    """
    Give back everything consumed under one fulfillment record.

    Nothing is deleted or edited: each original gets a matching storno entry, so the ledger still
    shows what happened and what undid it. Calling this twice is a no-op the second time — a
    transaction that already has its storno is left alone.

    Transactions whose wallet has since been archived are skipped: that wallet is gone from live
    storage and there is nothing left to credit.
    """
    at = at or utcnow()
    with store.unit_of_work():
        # Locking the record — not just the rows — is what serialises two concurrent reversals. Both
        # would otherwise read the same "not yet stornoed" set and each write a refund.
        if store.get_record(guid, lock=True) is None:
            raise RecordNotFoundError(f"No fulfillment record with guid {guid}")

        transactions = store.transactions_for_record(record_guid=guid, lock=True)
        reversed_ids = {t.storno_of_id for t in transactions if t.storno_of_id is not None}
        originals = [t for t in transactions if t.kind != TransactionKind.STORNO and t.id not in reversed_ids]

        for original in originals:
            wallet = store.get_wallet(original.wallet_id, lock=True)
            if wallet is None:
                continue
            amount = -original.amount
            store.create_transaction(
                wallet_id=wallet.id,
                kind=TransactionKind.STORNO,
                amount=amount,
                record_guid=guid,
                feature=original.feature,
                storno_of_id=original.id,
                at=at,
            )
            if not wallet.reset_config:
                store.save_wallet(replace(wallet, balance=wallet.balance + amount))


def credit(
    store: Store,
    wallet_id: object,
    amount,
    *,
    guid: uuid.UUID | None = None,
    obj: ObjectRef | None = None,
    at: datetime | None = None,
) -> Transaction:
    """
    Add ``amount`` to a wallet's balance, as a ``GRANT`` ledger entry.

    ``obj`` attaches the entry to the domain object that paid for it (a payment, an order). ``guid``
    files the entry under a fulfillment record, so :func:`unfulfill` can reverse it; a record whose
    entry has no ``obj`` is reported by :func:`find_unattached_fulfillment_records`.

    Raises :class:`~allowances.errors.WalletNotFoundError` for an unknown or archived wallet,
    :class:`~allowances.errors.UnsuitableWalletError` for a windowed or expired one, and
    ``ValueError`` for an amount that is not positive.
    """
    at = at or utcnow()
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError(f"A credit must be positive, got {amount}")
    with store.unit_of_work():
        wallet = store.get_wallet(wallet_id, lock=True)
        if wallet is None:
            raise WalletNotFoundError(f"No live wallet with id {wallet_id!r}")
        if wallet.reset_config:
            raise UnsuitableWalletError(f"Wallet {wallet_id!r} is windowed; its balance is a per-window allotment")
        if wallet.valid_until is not None and wallet.valid_until < at:
            raise UnsuitableWalletError(f"Wallet {wallet_id!r} expired at {wallet.valid_until.isoformat()}")

        record_guid = store.get_or_create_record(guid).guid if guid is not None else None
        txn = store.create_transaction(
            wallet_id=wallet.id,
            kind=TransactionKind.GRANT,
            amount=amount,
            record_guid=record_guid,
            at=at,
        )
        if obj is not None:
            store.attach_object(transaction_ids=[txn.id], obj=obj)
            txn = replace(txn, obj=obj)
        store.save_wallet(replace(wallet, balance=wallet.balance + amount))
    return txn


def _open(store: Store, subject: SubjectRef, spec: WalletSpec, source: str, valid_from: datetime) -> Wallet:
    return store.create_wallet(
        subject=subject,
        wallet_type=spec.wallet_type,
        currency=spec.currency,
        balance=spec.balance,
        overdraft_limit=spec.overdraft_limit,
        reset_config=spec.reset_config,
        valid_from=valid_from,
        valid_until=spec.valid_until,
        source=source,
        allotment=None if spec.on_switch == SwitchPolicy.REPLACE else spec.balance,
    )


def open_wallet(
    store: Store,
    subject: SubjectRef,
    spec: WalletSpec,
    *,
    source: str,
    effective_date: datetime | None = None,
) -> Wallet:
    """
    Open one wallet for ``subject`` from ``spec``, valid from ``effective_date`` (default now).

    ``source`` is stored on the wallet. A wallet with any source other than ``"plan"`` is never
    closed or adjusted by :func:`plan_switch` — that is how a purchased credit pack outlives plan
    changes.
    """
    with store.unit_of_work():
        return _open(store, subject, spec, source, effective_date or utcnow())


def _keepable(wallet: Wallet, spec: WalletSpec) -> bool:
    return (
        wallet.allotment is not None
        and wallet.wallet_type == spec.wallet_type
        and wallet.currency == spec.currency
        and bool(wallet.reset_config) == bool(spec.reset_config)
    )


def _reallot(store: Store, wallet: Wallet, new_amount: Decimal, top_up: bool, at: datetime, **changes) -> Wallet:
    """
    Give a wallet with an allotment a new one. A windowed wallet's balance is the allotment; a stored
    balance moves by the difference, booked as a ``GRANT`` entry.
    """
    if changes.get("reset_config", wallet.reset_config):
        balance, allotment = new_amount, new_amount
    else:
        allotment = new_amount
        delta = new_amount - wallet.allotment
        if top_up:
            delta = max(delta, Decimal("0"))
        if delta != 0:
            store.create_transaction(wallet_id=wallet.id, kind=TransactionKind.GRANT, amount=delta, at=at)
        balance = wallet.balance + delta
    adjusted = replace(wallet, balance=balance, allotment=allotment, **changes)
    store.save_wallet(adjusted)
    return adjusted


def _adjust(store: Store, wallet: Wallet, spec: WalletSpec, at: datetime) -> Wallet:
    return _reallot(
        store,
        wallet,
        Decimal(str(spec.balance)),
        spec.on_switch == SwitchPolicy.TOP_UP,
        at,
        overdraft_limit=spec.overdraft_limit,
        reset_config=spec.reset_config,
        valid_until=spec.valid_until,
    )


def adjust_wallet(store: Store, wallet_id: object, allotment, *, at: datetime | None = None) -> Wallet:
    """
    Change the allotment of a wallet that has one — a spending limit raised or lowered mid-period.

    Same arithmetic as :attr:`~allowances.types.SwitchPolicy.ADJUST`: a windowed wallet gets the new
    per-window allotment, a stored balance moves by the difference, which may be negative.

    Raises :class:`~allowances.errors.WalletNotFoundError` for an unknown or archived wallet and
    :class:`~allowances.errors.UnsuitableWalletError` for a wallet without an ``allotment``.
    """
    at = at or utcnow()
    with store.unit_of_work():
        wallet = store.get_wallet(wallet_id, lock=True)
        if wallet is None:
            raise WalletNotFoundError(f"No live wallet with id {wallet_id!r}")
        if wallet.allotment is None:
            raise UnsuitableWalletError(f"Wallet {wallet_id!r} has no allotment to adjust")
        return _reallot(store, wallet, Decimal(str(allotment)), False, at)


def consumed(store: Store, wallet_id: object, since: datetime, until: datetime) -> Decimal:
    """
    Net amount drawn from a wallet in ``[since, until]``: consumption minus the reversals of
    consumption booked in that interval. Credits, plan adjustments and their reversals do not count.
    This is the figure a host application bills for a postpaid budget wallet.
    """
    total = Decimal("0")
    for txn in store.transactions_for_wallet(wallet_id=wallet_id, since=since, until=until):
        # Consumption is always negative and a credit always positive, so a positive storno
        # reverses a consumption and a negative one reverses a credit.
        if txn.kind == TransactionKind.CONSUMPTION or (txn.kind == TransactionKind.STORNO and txn.amount > 0):
            total -= txn.amount
    return total


def plan_switch(
    store: Store,
    subjects: dict,
    open_wallets: Sequence[WalletSpec],
    effective_date: datetime | None = None,
) -> list[Wallet]:
    """
    Move subjects onto a different plan, atomically.

    Only wallets with ``source == "plan"`` take part. For each spec with ``on_switch`` ``ADJUST`` or
    ``TOP_UP``, the subject's current plan wallet of the same type and currency is kept and adjusted
    (see :class:`~allowances.types.SwitchPolicy`); every other current plan wallet is closed at
    ``effective_date`` and the remaining specs are opened from it.

    Each spec is applied once per subject. Returns the resulting wallets, kept or opened, per
    subject in spec order.

    A future ``effective_date`` schedules the switch. A switch with an ``ADJUST`` or ``TOP_UP``
    spec changes balances immediately and cannot be scheduled: it raises ``ValueError``.
    """
    now = utcnow()
    effective_date = effective_date or now
    keeps = [spec for spec in open_wallets if spec.on_switch != SwitchPolicy.REPLACE]
    if keeps and effective_date > now:
        raise ValueError("A plan switch with ADJUST or TOP_UP specs takes effect now and cannot be scheduled")

    result: list[Wallet] = []
    currencies = sorted({spec.currency for spec in keeps})
    with store.unit_of_work():
        for subject in subjects.values():
            current = []
            if currencies:
                current = [
                    wallet
                    for wallet in store.find_wallets(
                        subjects=[subject],
                        wallet_types=[WalletType.GRANT, WalletType.CONSUMABLE],
                        currencies=currencies,
                        at=effective_date,
                        lock=True,
                    )
                    if wallet.source == PLAN_SOURCE
                ]
            kept: dict[int, Wallet] = {}
            for index, spec in enumerate(open_wallets):
                if spec.on_switch == SwitchPolicy.REPLACE:
                    continue
                taken = {wallet.id for wallet in kept.values()}
                match = next((w for w in current if w.id not in taken and _keepable(w, spec)), None)
                if match is not None:
                    kept[index] = match

            store.close_wallets(
                subject=subject,
                at=effective_date,
                source=PLAN_SOURCE,
                exclude=[wallet.id for wallet in kept.values()],
            )
            for index, spec in enumerate(open_wallets):
                if index in kept:
                    result.append(_adjust(store, kept[index], spec, effective_date))
                else:
                    result.append(_open(store, subject, spec, PLAN_SOURCE, effective_date))
    return result


def find_unattached_fulfillment_records(store: Store, older_than: datetime | None = None):
    """
    Fulfillment records still holding a transaction with no domain object attached — quota that was
    spent on something that may never have happened. A recovery job walks these and decides whether
    to :func:`unfulfill` them.
    """
    return store.records_with_unattached_transactions(older_than=older_than)
