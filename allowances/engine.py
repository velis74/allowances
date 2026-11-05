"""
The fulfillment engine — ``peek``, ``fulfill``, ``attach``, ``unfulfill``, ``credit``,
``open_wallet``, ``adjust_wallet``, ``consumed``, ``plan_switch``, ``cancel_scheduled_switch``.

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
    wallet = _view(wallet, at)
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

    def current(found: list[Wallet]) -> list[Wallet]:
        # A plan switch that has come due is applied (when locked for writing) or only viewed.
        wallets = [_settle(store, w, at) if lock else _view(w, at) for w in found]
        return [w for w in wallets if w.is_valid_at(at)]

    if callable(feature_def.fulfillment_order):
        wallet_types = [wt for wt in (WalletType.GRANT, WalletType.CONSUMABLE) if feature_def.section_for(wt)]
        if not wallet_types:
            return []
        currencies: set[str] = set()
        for wallet_type in wallet_types:
            currencies.update(feature_def.section_for(wallet_type).currency_names)
        candidates = current(
            store.find_wallets(
                subjects=subject_refs,
                wallet_types=wallet_types,
                currencies=sorted(currencies),
                at=at,
                lock=lock,
            )
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
                found = current(
                    store.find_wallets(
                        subjects=[subject],
                        wallet_types=[wallet_type],
                        currencies=section.currency_names,
                        at=at,
                        lock=lock,
                    )
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
            wallet = _settle(store, wallet, at)
            if wallet.valid_until is None or wallet.valid_until > at:
                continue
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
            wallet = _settle(store, wallet, at)
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
    :class:`~allowances.errors.UnsuitableWalletError` for a windowed one or one not valid at ``at``, and
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
        wallet = _settle(store, wallet, at)
        if wallet.reset_config:
            raise UnsuitableWalletError(f"Wallet {wallet_id!r} is windowed; its balance is a per-window allotment")
        if not wallet.is_valid_at(at):
            raise UnsuitableWalletError(f"Wallet {wallet_id!r} is not valid at {at.isoformat()}")

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


def _open(
    store: Store,
    subject: SubjectRef,
    spec: WalletSpec,
    source: str,
    valid_from: datetime,
    scheduled: dict | None = None,
) -> Wallet:
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
        scheduled=scheduled,
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


def _reallotted(wallet: Wallet, new_amount: Decimal, top_up: bool, **changes) -> tuple[Wallet, Decimal]:
    """
    A wallet with a new allotment, and the change to its stored balance. A windowed wallet's balance
    is the allotment; a stored balance moves by the difference (``top_up``: only a positive one).
    """
    if changes.get("reset_config", wallet.reset_config):
        return replace(wallet, balance=new_amount, allotment=new_amount, **changes), Decimal("0")
    delta = new_amount - wallet.allotment
    if top_up:
        delta = max(delta, Decimal("0"))
    return replace(wallet, balance=wallet.balance + delta, allotment=new_amount, **changes), delta


def _reallot(store: Store, wallet: Wallet, new_amount: Decimal, top_up: bool, at: datetime, **changes) -> Wallet:
    """Persist :func:`_reallotted`, booking the balance change as a ``GRANT`` entry dated ``at``."""
    adjusted, delta = _reallotted(wallet, new_amount, top_up, **changes)
    if delta != 0:
        store.create_transaction(wallet_id=wallet.id, kind=TransactionKind.GRANT, amount=delta, at=at)
    store.save_wallet(adjusted)
    return adjusted


# -- scheduled plan switches ------------------------------------------------------------------------
#
# A switch with a future effective date records on each wallet it affects what it changes:
#   {"kind": "close", "at": ..., "valid_until": <the wallet's previous valid_until>}
#   {"kind": "open", "at": ...}
#   {"kind": "adjust", "at": ..., "allotment": ..., "top_up": ..., <spec fields>}
# Closing and opening are expressed by validity dates already; "adjust" is applied lazily — viewed
# by readers from its date on, and written by the first locked operation that reaches the wallet.


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _from_iso(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _scheduled_at(wallet: Wallet) -> datetime:
    return datetime.fromisoformat(wallet.scheduled["at"])


def _adjust_marker(spec: WalletSpec, at: datetime) -> dict:
    return {
        "kind": "adjust",
        "at": at.isoformat(),
        "allotment": str(spec.balance),
        "top_up": spec.on_switch == SwitchPolicy.TOP_UP,
        "overdraft_limit": None if spec.overdraft_limit is None else str(spec.overdraft_limit),
        "reset_config": spec.reset_config,
        "valid_until": _iso(spec.valid_until),
    }


def _marker_args(marker: dict) -> tuple[Decimal, bool, dict]:
    changes = {
        "overdraft_limit": None if marker["overdraft_limit"] is None else Decimal(marker["overdraft_limit"]),
        "reset_config": marker["reset_config"],
        "valid_until": _from_iso(marker["valid_until"]),
    }
    return Decimal(marker["allotment"]), marker["top_up"], changes


def _due_adjustment(wallet: Wallet, at: datetime) -> bool:
    return bool(wallet.scheduled) and wallet.scheduled["kind"] == "adjust" and _scheduled_at(wallet) <= at


def _view(wallet: Wallet, at: datetime) -> Wallet:
    """The wallet as it is at ``at``, with a scheduled adjustment that has come due applied, unsaved."""
    if not _due_adjustment(wallet, at):
        return wallet
    amount, top_up, changes = _marker_args(wallet.scheduled)
    return _reallotted(replace(wallet, scheduled=None), amount, top_up, **changes)[0]


def _settle(store: Store, wallet: Wallet, at: datetime) -> Wallet:
    """Write a scheduled adjustment that has come due, dated on its effective date."""
    if not _due_adjustment(wallet, at):
        return wallet
    amount, top_up, changes = _marker_args(wallet.scheduled)
    return _reallot(store, replace(wallet, scheduled=None), amount, top_up, _scheduled_at(wallet), **changes)


def _revert(store: Store, wallet: Wallet, now: datetime) -> None:
    """Undo what a not-yet-effective switch recorded on ``wallet``."""
    kind = wallet.scheduled["kind"]
    if kind == "close":
        store.save_wallet(replace(wallet, valid_until=_from_iso(wallet.scheduled["valid_until"]), scheduled=None))
    elif kind == "open":
        # Ended before it began: never valid, and swept by archive_expired_wallets.
        store.save_wallet(replace(wallet, valid_until=now, scheduled=None))
    else:
        store.save_wallet(replace(wallet, scheduled=None))


def _resolve_scheduled(store: Store, subject: SubjectRef, now: datetime, cancel: bool) -> bool:
    """
    Settle the subject's switch records that have come due; with ``cancel``, revert the ones that
    have not. Returns whether anything was reverted.
    """
    reverted = False
    for wallet in store.find_scheduled_wallets(subject=subject, lock=True):
        if _scheduled_at(wallet) > now:
            if cancel:
                _revert(store, wallet, now)
                reverted = True
        elif wallet.scheduled["kind"] == "adjust":
            _settle(store, wallet, now)
        else:
            store.save_wallet(replace(wallet, scheduled=None))
    return reverted


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
        wallet = _settle(store, wallet, at)
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
    ``TOP_UP``, the subject's plan wallet of the same type and currency is kept and adjusted (see
    :class:`~allowances.types.SwitchPolicy`); every other plan wallet valid at ``effective_date`` is
    closed then, and the remaining specs are opened from then.

    A future ``effective_date`` schedules the switch: closing and opening take effect through the
    wallets' validity dates, an adjustment applies on that date. A scheduled switch can be cancelled
    with :func:`cancel_scheduled_switch` until it takes effect, and a new switch for the same
    subject replaces it.

    Each spec is applied once per subject. Returns the resulting wallets, kept or opened, per
    subject in spec order.
    """
    now = utcnow()
    effective_date = effective_date or now
    scheduled = effective_date > now

    result: list[Wallet] = []
    with store.unit_of_work():
        for subject in subjects.values():
            _resolve_scheduled(store, subject, now, cancel=True)
            current = [
                wallet
                for wallet in store.find_wallets(
                    subjects=[subject],
                    wallet_types=[WalletType.GRANT, WalletType.CONSUMABLE],
                    currencies=None,
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

            kept_ids = {wallet.id for wallet in kept.values()}
            for wallet in current:
                if wallet.id in kept_ids:
                    continue
                marker = (
                    {"kind": "close", "at": effective_date.isoformat(), "valid_until": _iso(wallet.valid_until)}
                    if scheduled
                    else None
                )
                store.save_wallet(replace(wallet, valid_until=effective_date, scheduled=marker))

            for index, spec in enumerate(open_wallets):
                if index in kept and scheduled:
                    wallet = replace(kept[index], scheduled=_adjust_marker(spec, effective_date))
                    store.save_wallet(wallet)
                    result.append(wallet)
                elif index in kept:
                    result.append(_adjust(store, kept[index], spec, effective_date))
                else:
                    marker = {"kind": "open", "at": effective_date.isoformat()} if scheduled else None
                    result.append(_open(store, subject, spec, PLAN_SOURCE, effective_date, scheduled=marker))
    return result


def cancel_scheduled_switch(store: Store, subjects: dict) -> int:
    """
    Cancel the plan switch scheduled for these subjects that has not yet taken effect: closed wallets
    get their previous ``valid_until`` back, scheduled adjustments are dropped, and wallets it would
    have opened end before they begin. Returns how many subjects had a switch cancelled.
    """
    now = utcnow()
    cancelled = 0
    with store.unit_of_work():
        for subject in subjects.values():
            if _resolve_scheduled(store, subject, now, cancel=True):
                cancelled += 1
    return cancelled


def find_unattached_fulfillment_records(store: Store, older_than: datetime | None = None):
    """
    Fulfillment records still holding a transaction with no domain object attached — quota that was
    spent on something that may never have happened. A recovery job walks these and decides whether
    to :func:`unfulfill` them.
    """
    return store.records_with_unattached_transactions(older_than=older_than)
