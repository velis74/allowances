"""
The fulfillment engine — ``peek``, ``fulfill``, ``attach``, ``unfulfill``, ``credit``, ``transfer``,
``open_wallet``, ``adjust_wallet``, ``close_wallet``, ``consumed``, ``plan_switch``,
``cancel_scheduled_switch``, ``effective_allowances``, and the membership functions ``add_member``, ``remove_member``,
``groups_of``.

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
    MembershipCycleError,
    RecordNotFoundError,
    UnsuitableWalletError,
    WalletNotFoundError,
)
from allowances.rounding import apply_round
from allowances.store import Store
from allowances.types import (
    Allowance,
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


_ZERO = Decimal("0")


def _negated(amount: Decimal) -> Decimal:
    """``-amount``, with zero kept positive."""
    return _ZERO - amount


def _same_id(a: object, b: object) -> bool:
    return a == b or str(a) == str(b)


def _lock(store: Store, wallet_ids: Iterable[object]) -> dict:
    """Lock the wallets in one :meth:`~allowances.store.Store.lock_wallets` call; their state by id."""
    ids = list(dict.fromkeys(wallet_ids))
    if not ids:
        return {}
    return {wallet.id: wallet for wallet in store.lock_wallets(ids)}


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


def _expand(store: Store, subjects: dict) -> dict[str, list[SubjectRef]]:
    """
    Each caller-supplied subject with its transitive groups, under the same key: the subject, then
    its direct groups, then theirs, breadth-first and deduplicated per key.
    """
    expanded = {key: [subject] for key, subject in subjects.items()}
    seen = {key: {subject} for key, subject in subjects.items()}
    frontiers = {key: [subject] for key, subject in subjects.items()}
    while any(frontiers.values()):
        members = list(dict.fromkeys(m for frontier in frontiers.values() for m in frontier))
        groups_by_member: dict[SubjectRef, list[SubjectRef]] = {}
        for member, group in store.memberships_of(members=members):
            groups_by_member.setdefault(member, []).append(group)
        for key, frontier in frontiers.items():
            following = []
            for member in frontier:
                for group in groups_by_member.get(member, []):
                    if group not in seen[key]:
                        seen[key].add(group)
                        expanded[key].append(group)
                        following.append(group)
            frontiers[key] = following
    return expanded


def _all_subjects(expanded: dict[str, list[SubjectRef]]) -> list[SubjectRef]:
    return list(dict.fromkeys(subject for refs in expanded.values() for subject in refs))


def _ordered_wallets(store: Store, feature_def: FeatureDefinition, expanded: dict, at: datetime) -> list[Wallet]:
    """
    Expand ``fulfillment_order`` into a concrete, deduplicated list of candidate wallets in the
    order they should be drawn from, as they are at ``at``. Nothing is locked or written.

    A callable ``fulfillment_order`` gets one flat list of every plausible candidate of every
    expanded subject and decides the order itself. A list of steps is walked in declaration order;
    a step naming a key walks that key's subjects in expansion order, a step without one walks
    every expanded subject. Within one subject, wallets that expire soonest come first, wallets
    without ``valid_until`` last, and equal expiries oldest-first.
    """
    subject_refs = _all_subjects(expanded)
    if not subject_refs:
        return []

    def current(found: list[Wallet]) -> list[Wallet]:
        wallets = [_view(w, at) for w in found]
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
        else:
            step_subjects = expanded.get(step_subject, [])
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
                    )
                )
                for wallet in sorted(found, key=_expiry_key):
                    if step_source is not None and wallet.source != step_source:
                        continue
                    if wallet.id not in seen:
                        seen.add(wallet.id)
                        ordered.append(wallet)
    return ordered


def _locked_candidates(store: Store, candidates: list[Wallet], at: datetime) -> list[Wallet]:
    """
    Lock every candidate in one pass and return their current state in draw order, with a plan
    switch that has come due written, and without wallets no longer valid at ``at``.
    """
    locked = _lock(store, [w.id for w in candidates])
    result = []
    for wallet in candidates:
        fresh = locked.get(wallet.id)
        if fresh is None:
            continue
        fresh = _settle(store, fresh, at)
        if fresh.is_valid_at(at):
            result.append(fresh)
    return result


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

    A wallet whose effective rate is zero is a right: with a positive balance it covers the whole
    remainder at cost zero, and it never takes part in the overdraft pass.
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
        effective_rate = rate * modifier_factor

        if effective_rate == 0:
            if not use_overdraft and balance > 0:
                drawn.append(_Draw(wallet=wallet, cost=_ZERO, qty=remaining))
                remaining = _ZERO
            continue

        if use_overdraft:
            # Only wallets that were given an explicit overdraft limit take part in the second pass.
            if wallet.overdraft_limit is None:
                continue
            capacity = max(balance, Decimal("0")) + wallet.overdraft_limit
        else:
            capacity = max(balance, Decimal("0"))
        if capacity <= 0:
            continue

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

        qty_covered = min(cost / effective_rate, remaining)

        drawn.append(_Draw(wallet=wallet, cost=cost, qty=qty_covered))
        pending[wallet.id] = already + cost
        remaining -= qty_covered

    return drawn, remaining


def _modifier_factor(store: Store, feature: str, subjects: list[SubjectRef], at: datetime) -> Decimal:
    """Where several modifiers apply, the subject gets the most favourable one."""
    if not subjects:
        return Decimal("1")
    factors = store.find_modifier_factors(feature=feature, subjects=subjects, at=at)
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

    ``subjects`` are expanded with their groups. With ``lock`` every candidate wallet is locked in
    one pass before any balance is read, and a plan switch that has come due is written.
    """
    pending: dict = {}
    expanded = _expand(store, subjects)
    candidates = _ordered_wallets(store, feature_def, expanded, at)
    if lock:
        candidates = _locked_candidates(store, candidates, at)
    factor = _modifier_factor(store, feature, _all_subjects(expanded), at)

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
        expired = store.find_expired_wallets(at=at)
        locked = _lock(store, [w.id for w in expired])
        for found in expired:
            wallet = locked.get(found.id)
            if wallet is None:
                continue
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
    ``subjects`` are expanded with their groups, as in :func:`fulfill`.
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
    *,
    actor: SubjectRef | None = None,
) -> FulfillmentResult:
    """
    Actually consume the licence, writing one transaction per wallet drawn from.

    With ``allow_partial=False`` (the default) a request that cannot be met in full raises
    :class:`~allowances.errors.InsufficientBalanceError` and the whole call is rolled back — the
    caller gets all of it or none of it. With ``allow_partial=True`` whatever was available is
    consumed and ``result.satisfied`` reports whether that was enough.

    Passing a ``guid`` groups this call with earlier ones under a single fulfillment record, so one
    business operation spanning several features can be reversed as a unit.

    Every subject in ``subjects`` is expanded with its transitive groups under the same key (see
    :func:`groups_of`). A wallet with an effective rate of zero and a positive balance (a right)
    covers the rest of the request; its use is recorded as a consumption entry of amount zero.

    ``actor`` is recorded on every entry. Without one, the actor is the single subject of
    ``subjects`` when it holds exactly one, else ``None``.

    Every candidate wallet is locked in one pass, in ascending id order, before balances are read.
    """
    feature_def = provider.get_feature(feature)
    at = at or utcnow()
    quantity = Decimal(str(quantity))
    subjects = subjects or {}
    if actor is None and len(subjects) == 1:
        actor = next(iter(subjects.values()))

    with store.unit_of_work():
        record = _record(store, guid)
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
                    amount=_negated(draw.cost),
                    record_guid=record.guid,
                    feature=feature,
                    at=at,
                    actor=actor,
                )
            )
            if draw.cost == 0:
                continue

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


def _record(store: Store, guid: uuid.UUID | None):
    """The record for ``guid`` (a new one for ``None``); an existing record is locked."""
    record = store.get_or_create_record(guid)
    if guid is not None:
        store.get_record(record.guid, lock=True)
    return record


def attach(store: Store, guid: uuid.UUID, obj: ObjectRef) -> None:
    """
    Record which domain object a fulfillment paid for.

    Fulfillment happens before the business operation completes — you consume the quota, then send
    the email. This closes the loop afterwards, and it is what makes a record with unattached
    consumption entries a usable signal that something died in between.
    """
    with store.unit_of_work():
        if store.get_record(guid) is None:
            raise RecordNotFoundError(f"No fulfillment record with guid {guid}")
        pending = [t.id for t in store.transactions_for_record(record_guid=guid) if t.obj is None]
        if pending:
            store.attach_object(transaction_ids=pending, obj=obj)


def _refuse_archived_source(wallet_id: object) -> None:
    where = f" {wallet_id!r}" if wallet_id is not None else ""
    raise WalletNotFoundError(f"The source wallet{where} of a transfer is archived; the transfer cannot be reversed")


def unfulfill(store: Store, guid: uuid.UUID, at: datetime | None = None, *, actor: SubjectRef | None = None) -> None:
    """
    Give back everything recorded under one fulfillment record: consumption, credits and transfers.

    Nothing is deleted or edited: each original gets a matching storno entry carrying ``actor`` (who
    reversed it), so the ledger still shows what happened and what undid it. Calling this twice is a
    no-op the second time — a transaction that already has its storno is left alone.

    Reversing a transfer takes the amount back from its target. The check nets every storno of the
    record on that wallet, so a consumption of the transferred amount under the same guid is given
    back first. When the target would still end below zero (it is archived, or
    ``max(effective_balance, 0)`` plus the record's net change is negative),
    :class:`~allowances.errors.InsufficientBalanceError` is raised and nothing is written. When the
    source of a transfer is archived, :class:`~allowances.errors.WalletNotFoundError` is raised and
    nothing is written.

    Other transactions whose wallet has since been archived are skipped: that wallet is gone from
    live storage and there is nothing left to credit.

    The record is locked first, then every wallet involved in one pass, in ascending id order.
    """
    at = at or utcnow()
    with store.unit_of_work():
        # Locking the record — not just the rows — is what serialises two concurrent reversals. Both
        # would otherwise read the same "not yet stornoed" set and each write a refund.
        if store.get_record(guid, lock=True) is None:
            raise RecordNotFoundError(f"No fulfillment record with guid {guid}")

        # Read without row locks: the record lock serialises reversals, and wallets are locked before rows.
        transactions = store.transactions_for_record(record_guid=guid)
        reversed_ids = {t.storno_of_id for t in transactions if t.storno_of_id is not None}
        originals = [t for t in transactions if t.kind != TransactionKind.STORNO and t.id not in reversed_ids]

        wallets = {
            wallet_id: _settle(store, wallet, at)
            for wallet_id, wallet in _lock(store, [t.wallet_id for t in originals]).items()
        }

        net: dict = {}
        owed: dict = {}
        unpaired: dict = {}
        for original in originals:
            net[original.wallet_id] = net.get(original.wallet_id, _ZERO) - original.amount
            if original.kind != TransactionKind.TRANSFER:
                continue
            # Archiving a wallet removes its entries from the record, so a leg without its
            # counterpart means the other wallet is archived.
            unpaired[abs(original.amount)] = unpaired.get(abs(original.amount), 0) + (1 if original.amount > 0 else -1)
            if original.amount > 0:
                owed[original.wallet_id] = owed.get(original.wallet_id, _ZERO) + original.amount
            elif original.wallet_id not in wallets:
                _refuse_archived_source(original.wallet_id)
        for amount, surplus in unpaired.items():
            if surplus > 0:
                _refuse_archived_source(None)
            if surplus < 0:
                raise InsufficientBalanceError(
                    FulfillmentResult(requested=amount, granted=_ZERO, satisfied=False, transactions=[], guid=guid)
                )
        for wallet_id, amount in owed.items():
            wallet = wallets.get(wallet_id)
            available = max(effective_balance(store, wallet, at), _ZERO) if wallet is not None else _ZERO
            if wallet is None or available + net[wallet_id] < 0:
                raise InsufficientBalanceError(
                    FulfillmentResult(
                        requested=amount if wallet is None else _negated(net[wallet_id]),
                        granted=available,
                        satisfied=False,
                        transactions=[],
                        guid=guid,
                    )
                )

        for original in originals:
            wallet = wallets.get(original.wallet_id)
            if wallet is None:
                continue
            amount = _negated(original.amount)
            store.create_transaction(
                wallet_id=wallet.id,
                kind=TransactionKind.STORNO,
                amount=amount,
                record_guid=guid,
                feature=original.feature,
                storno_of_id=original.id,
                at=at,
                actor=actor,
            )
            if not wallet.reset_config and amount != 0:
                wallet = replace(wallet, balance=wallet.balance + amount)
                store.save_wallet(wallet)
                wallets[wallet.id] = wallet


def credit(
    store: Store,
    wallet_id: object,
    amount,
    *,
    guid: uuid.UUID | None = None,
    obj: ObjectRef | None = None,
    actor: SubjectRef | None = None,
    at: datetime | None = None,
) -> Transaction:
    """
    Add ``amount`` to a wallet's balance, as a ``GRANT`` ledger entry recorded with ``actor``.

    ``obj`` attaches the entry to the domain object that paid for it (a payment, an order). ``guid``
    files the entry under a fulfillment record, so :func:`unfulfill` can reverse it.

    Raises :class:`~allowances.errors.WalletNotFoundError` for an unknown or archived wallet,
    :class:`~allowances.errors.UnsuitableWalletError` for a windowed one or one not valid at ``at``, and
    ``ValueError`` for an amount that is not positive.
    """
    at = at or utcnow()
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError(f"A credit must be positive, got {amount}")
    with store.unit_of_work():
        record_guid = _record(store, guid).guid if guid is not None else None
        wallet = store.get_wallet(wallet_id, lock=True)
        if wallet is None:
            raise WalletNotFoundError(f"No live wallet with id {wallet_id!r}")
        wallet = _settle(store, wallet, at)
        if wallet.reset_config:
            raise UnsuitableWalletError(f"Wallet {wallet_id!r} is windowed; its balance is a per-window allotment")
        if not wallet.is_valid_at(at):
            raise UnsuitableWalletError(f"Wallet {wallet_id!r} is not valid at {at.isoformat()}")

        txn = store.create_transaction(
            wallet_id=wallet.id,
            kind=TransactionKind.GRANT,
            amount=amount,
            record_guid=record_guid,
            at=at,
            actor=actor,
        )
        if obj is not None:
            store.attach_object(transaction_ids=[txn.id], obj=obj)
            txn = replace(txn, obj=obj)
        store.save_wallet(replace(wallet, balance=wallet.balance + amount))
    return txn


def transfer(
    store: Store,
    from_wallet_id: object,
    to_wallet_id: object,
    amount,
    *,
    guid: uuid.UUID | None = None,
    obj: ObjectRef | None = None,
    actor: SubjectRef | None = None,
    at: datetime | None = None,
) -> FulfillmentResult:
    """
    Move ``amount`` from one wallet to another of the same currency.

    Writes two ``TRANSFER`` entries under one fulfillment record (``guid``, or a new one): a debit
    on the source and a credit on the target, both recorded with ``actor`` and attached to ``obj``
    when given. The wallet types may differ. A windowed wallet's stored allotment is not changed;
    the entries count in its window like any other. :func:`unfulfill` reverses both legs.

    The source must hold the amount (``max(effective_balance, 0) >= amount``); overdraft is never
    used. Both wallets are locked in one pass, in ascending id order.

    Raises ``ValueError`` for an amount that is not positive,
    :class:`~allowances.errors.WalletNotFoundError` for an unknown or archived wallet,
    :class:`~allowances.errors.UnsuitableWalletError` for the same wallet on both sides, different
    currencies, or a wallet not valid at ``at``, and
    :class:`~allowances.errors.InsufficientBalanceError` when the source does not hold the amount.
    Nothing is written when it raises.
    """
    at = at or utcnow()
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError(f"A transfer must be positive, got {amount}")
    if _same_id(from_wallet_id, to_wallet_id):
        raise UnsuitableWalletError(f"Cannot transfer from wallet {from_wallet_id!r} to itself")

    with store.unit_of_work():
        record = _record(store, guid)
        locked = store.lock_wallets([from_wallet_id, to_wallet_id])
        source = next((w for w in locked if _same_id(w.id, from_wallet_id)), None)
        target = next((w for w in locked if _same_id(w.id, to_wallet_id)), None)
        for wallet, wallet_id in ((source, from_wallet_id), (target, to_wallet_id)):
            if wallet is None:
                raise WalletNotFoundError(f"No live wallet with id {wallet_id!r}")
        source = _settle(store, source, at)
        target = _settle(store, target, at)
        if source.currency != target.currency:
            raise UnsuitableWalletError(
                f"Wallet {from_wallet_id!r} holds {source.currency!r}, wallet {to_wallet_id!r} {target.currency!r}"
            )
        for wallet in (source, target):
            if not wallet.is_valid_at(at):
                raise UnsuitableWalletError(f"Wallet {wallet.id!r} is not valid at {at.isoformat()}")

        available = max(effective_balance(store, source, at), _ZERO)
        if available < amount:
            raise InsufficientBalanceError(
                FulfillmentResult(
                    requested=amount,
                    granted=available,
                    satisfied=False,
                    transactions=[],
                    guid=record.guid,
                )
            )

        legs = []
        for wallet, delta in ((source, _negated(amount)), (target, amount)):
            legs.append(
                store.create_transaction(
                    wallet_id=wallet.id,
                    kind=TransactionKind.TRANSFER,
                    amount=delta,
                    record_guid=record.guid,
                    at=at,
                    actor=actor,
                )
            )
            if not wallet.reset_config:
                store.save_wallet(replace(wallet, balance=wallet.balance + delta))
        if obj is not None:
            store.attach_object(transaction_ids=[t.id for t in legs], obj=obj)
            legs = [replace(t, obj=obj) for t in legs]

    return FulfillmentResult(
        requested=amount,
        granted=amount,
        satisfied=True,
        transactions=legs,
        guid=record.guid,
    )


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


def _resolve_scheduled(store: Store, wallets: Iterable[Wallet], now: datetime, cancel: bool) -> bool:
    """
    Settle the switch records on ``wallets`` (locked by the caller) that have come due; with
    ``cancel``, revert the ones that have not. Returns whether anything was reverted.
    """
    reverted = False
    for wallet in wallets:
        if not wallet.scheduled:
            continue
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


def close_wallet(store: Store, wallet_id: object, *, at: datetime | None = None) -> Wallet:
    """
    End a wallet at ``at`` (default now) — revoking a right, closing a budget.

    A scheduled plan-switch adjustment already due at ``at`` is applied first. The wallet's
    ``valid_until`` becomes ``at`` unless it already ends earlier, and a scheduled switch record not
    yet effective is dropped. No ledger entry is written and the balance is unchanged;
    :func:`archive_expired_wallets` archives the wallet later. Returns the closed wallet.

    Raises :class:`~allowances.errors.WalletNotFoundError` for an unknown or archived wallet.
    """
    at = at or utcnow()
    with store.unit_of_work():
        locked = store.lock_wallets([wallet_id])
        if not locked:
            raise WalletNotFoundError(f"No live wallet with id {wallet_id!r}")
        wallet = _settle(store, locked[0], at)
        valid_until = wallet.valid_until if wallet.valid_until is not None and wallet.valid_until <= at else at
        wallet = replace(wallet, valid_until=valid_until, scheduled=None)
        store.save_wallet(wallet)
        return wallet


def consumed(store: Store, wallet_id: object, since: datetime, until: datetime) -> Decimal:
    """
    Net amount drawn from a wallet in ``[since, until]``: consumption minus the reversals of
    consumption booked in that interval. Credits, transfers, plan adjustments and their reversals do
    not count. This is the figure a host application bills for a postpaid budget wallet.
    """
    total = Decimal("0")
    kinds: dict = {}
    for txn in store.transactions_for_wallet(wallet_id=wallet_id, since=since, until=until):
        if txn.kind == TransactionKind.CONSUMPTION:
            total -= txn.amount
        elif txn.kind == TransactionKind.STORNO and txn.amount > 0:
            # A positive storno reverses a consumption or a transfer debit; only the first counts.
            if txn.record_guid is not None and txn.record_guid not in kinds:
                kinds[txn.record_guid] = {
                    t.id: t.kind for t in store.transactions_for_record(record_guid=txn.record_guid)
                }
            original_kind = kinds.get(txn.record_guid, {}).get(txn.storno_of_id, TransactionKind.CONSUMPTION)
            if original_kind == TransactionKind.CONSUMPTION:
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

    Each spec is applied once per subject, also when the same subject is given under several keys.
    Returns the resulting wallets, kept or opened, per subject in spec order.

    Every wallet of the subjects that the switch may change is locked in one pass, in ascending id
    order, before any is changed.
    """
    now = utcnow()
    effective_date = effective_date or now
    scheduled = effective_date > now
    both_types = [WalletType.GRANT, WalletType.CONSUMABLE]

    # A subject given under several keys is switched once.
    unique_subjects = list(dict.fromkeys(subjects.values()))
    result: list[Wallet] = []
    with store.unit_of_work():
        pending = {subject: store.find_scheduled_wallets(subject=subject) for subject in unique_subjects}
        candidate_ids = [w.id for wallets in pending.values() for w in wallets]
        for subject in unique_subjects:
            candidate_ids += [
                w.id
                for w in store.find_wallets(
                    subjects=[subject], wallet_types=both_types, currencies=None, at=effective_date
                )
            ]
        locked = _lock(store, candidate_ids)

        for subject in unique_subjects:
            # Read under the locks; a wallet found only after locking was not locked and is left alone.
            scheduled_now = [w for w in store.find_scheduled_wallets(subject=subject) if w.id in locked]
            _resolve_scheduled(store, scheduled_now, now, cancel=True)
            current = [
                wallet
                for wallet in store.find_wallets(
                    subjects=[subject], wallet_types=both_types, currencies=None, at=effective_date
                )
                if wallet.source == PLAN_SOURCE and wallet.id in locked
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
        unique_subjects = list(dict.fromkeys(subjects.values()))
        found = [w.id for subject in unique_subjects for w in store.find_scheduled_wallets(subject=subject)]
        locked = _lock(store, found)
        for subject in unique_subjects:
            # Read under the locks; a wallet found only after locking was not locked and is left alone.
            wallets = [w for w in store.find_scheduled_wallets(subject=subject) if w.id in locked]
            if _resolve_scheduled(store, wallets, now, cancel=True):
                cancelled += 1
    return cancelled


def effective_allowances(
    store: Store,
    subject: SubjectRef,
    *,
    at: datetime | None = None,
    wallet_types: Sequence[WalletType] | None = None,
    currencies: Sequence[str] | None = None,
) -> list[Allowance]:
    """
    Every wallet valid at ``at`` (default now) of ``subject`` and of the groups it belongs to,
    directly or transitively, with its holder, depth and effective balance.

    ``wallet_types`` defaults to both types, ``currencies`` ``None`` to every currency. Wallets are
    listed whatever their balance, zero and negative included. Ordered by depth, then by holder in
    :func:`groups_of` order, then by the store's wallet order. Nothing is locked or written.
    """
    at = at or utcnow()
    levels = _levels(store, subject)
    holders = [holder for level in levels for holder in level]
    depths = {holder: depth for depth, level in enumerate(levels) for holder in level}
    position = {holder: index for index, holder in enumerate(holders)}
    found = store.find_wallets(
        subjects=holders,
        wallet_types=list(wallet_types) if wallet_types is not None else [WalletType.GRANT, WalletType.CONSUMABLE],
        currencies=None if currencies is None else list(currencies),
        at=at,
    )
    wallets = [w for w in (_view(w, at) for w in found) if w.is_valid_at(at)]
    wallets.sort(key=lambda w: (depths[w.subject], position[w.subject]))
    return [
        Allowance(wallet=w, holder=w.subject, depth=depths[w.subject], balance=effective_balance(store, w, at))
        for w in wallets
    ]


def find_unattached_fulfillment_records(store: Store, older_than: datetime | None = None):
    """
    Fulfillment records still holding a consumption entry with no domain object attached — quota
    that was spent on something that may never have happened. A recovery job walks these and decides
    whether to :func:`unfulfill` them. Credits and transfers are complete when written and never make
    a record appear here.
    """
    return store.records_with_unattached_transactions(older_than=older_than)


# -------------------------------------------------------------------------------------------------
# Membership
# -------------------------------------------------------------------------------------------------


def groups_of(store: Store, subject: SubjectRef) -> list[SubjectRef]:
    """
    Every group ``subject`` is a member of, directly or through other groups: direct groups first,
    then their groups, breadth-first, each group once. Terminates on cyclic data; ``subject`` itself
    is never included.
    """
    return [group for level in _levels(store, subject)[1:] for group in level]


def _levels(store: Store, subject: SubjectRef) -> list[list[SubjectRef]]:
    """``subject``, then its direct groups, then theirs, one list per level, each group once."""
    seen = {subject}
    levels = [[subject]]
    while True:
        groups_by_member: dict[SubjectRef, list[SubjectRef]] = {}
        for member, group in store.memberships_of(members=levels[-1]):
            groups_by_member.setdefault(member, []).append(group)
        following = []
        for member in levels[-1]:
            for group in groups_by_member.get(member, []):
                if group not in seen:
                    seen.add(group)
                    following.append(group)
        if not following:
            return levels
        levels.append(following)


def add_member(store: Store, member: SubjectRef, group: SubjectRef) -> None:
    """
    Make ``member`` a direct member of ``group``. An existing membership is left as it is.

    Membership changes are serialised (:meth:`~allowances.store.Store.lock_memberships`) so two
    concurrent additions cannot form a cycle together. Raises
    :class:`~allowances.errors.MembershipCycleError` when ``member`` is ``group`` or ``group`` is
    already, directly or transitively, a member of ``member``.
    """
    with store.unit_of_work():
        store.lock_memberships()
        if member == group or member in groups_of(store, group):
            raise MembershipCycleError(f"{member} cannot become a member of {group}: it would form a cycle")
        if (member, group) in store.memberships_of(members=[member]):
            return
        store.add_membership(member=member, group=group)


def remove_member(store: Store, member: SubjectRef, group: SubjectRef) -> bool:
    """Remove the direct membership of ``member`` in ``group``. Returns whether it existed."""
    with store.unit_of_work():
        store.lock_memberships()
        return store.remove_membership(member=member, group=group)
