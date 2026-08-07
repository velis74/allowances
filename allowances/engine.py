"""
The fulfillment engine — ``peek``, ``fulfill``, ``attach``, ``unfulfill``, ``plan_switch``.

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
from allowances.errors import InsufficientBalanceError, RecordNotFoundError
from allowances.rounding import apply_round
from allowances.store import Store
from allowances.types import (
    FulfillmentResult,
    ObjectRef,
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
    the order itself. A list of steps is walked in declaration order; within a step, wallets come
    back oldest-first, which is what makes soon-to-expire grants get spent before fresh ones.
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
    for step_type, step_subject in feature_def.fulfillment_order:
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
                for wallet in store.find_wallets(
                    subjects=[subject],
                    wallet_types=[wallet_type],
                    currencies=section.currency_names,
                    at=at,
                    lock=lock,
                ):
                    if wallet.id not in seen:
                        seen.add(wallet.id)
                        ordered.append(wallet)
    return ordered


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
    the next request.
    """
    if wallet.reset_config:
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


def plan_switch(
    store: Store,
    subjects: dict,
    open_wallets: Sequence[WalletSpec],
    effective_date: datetime | None = None,
) -> list[Wallet]:
    """
    Move subjects onto a different plan: close every wallet they currently hold and open the new
    set, atomically.

    Each spec is opened once per subject, so switching a user *and* their organisation onto the
    same plan in one call gives each their own wallets. Returns the wallets opened.
    """
    effective_date = effective_date or utcnow()
    opened: list[Wallet] = []
    with store.unit_of_work():
        for subject in subjects.values():
            store.close_wallets(subject=subject, at=effective_date)
            for spec in open_wallets:
                opened.append(
                    store.create_wallet(
                        subject=subject,
                        wallet_type=spec.wallet_type,
                        currency=spec.currency,
                        balance=spec.balance,
                        overdraft_limit=spec.overdraft_limit,
                        reset_config=spec.reset_config,
                        valid_from=effective_date,
                        valid_until=spec.valid_until,
                    )
                )
    return opened


def find_unattached_fulfillment_records(store: Store, older_than: datetime | None = None):
    """
    Fulfillment records still holding a transaction with no domain object attached — quota that was
    spent on something that may never have happened. A recovery job walks these and decides whether
    to :func:`unfulfill` them.
    """
    return store.records_with_unattached_transactions(older_than=older_than)
