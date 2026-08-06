"""
The fulfillment engine — concern 2 of django-licensing.

Implements the README "Core API" (``peek``/``fulfill``/``attach``/``unfulfill``/``plan_switch``) on
top of the DB schema built in concern 1 (``models.py``) and the definition layer read through a
``DefinitionProvider`` (``providers.py``). See ``DECISIONS.md`` (entries from concern 2 onward) for
every point where the README is silent or ambiguous and a concrete choice had to be made.
"""

import calendar

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from django.contrib.contenttypes.models import ContentType
from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.utils import timezone

from django_licensing.models import (
    FulfillmentRecord,
    LicenseModifier,
    Transaction,
    TransactionArchive,
    TransactionKind,
    Wallet,
    WalletArchive,
    WalletType,
)
from django_licensing.providers import DefinitionProvider, FeatureDefinition, get_default_provider

# ---------------------------------------------------------------------------------------------
# FulfillmentResult / errors
# ---------------------------------------------------------------------------------------------


@dataclass
class FulfillmentResult:
    """README "Core API" → FulfillmentResult. Returned by both ``peek`` and ``fulfill``."""

    requested: Decimal
    granted: Decimal
    satisfied: bool
    transactions: list
    guid: object  # UUID for fulfill(); None for peek(), which never creates a FulfillmentRecord.
    modifier_applied: Decimal | None = None


class InsufficientBalanceError(Exception):
    """
    Raised by ``fulfill(..., allow_partial=False)`` when the request cannot be fully satisfied.

    Carries the ``FulfillmentResult`` that *would* have been returned had ``allow_partial=True``
    been passed (``transactions`` empty, since — per ``allow_partial=False`` semantics — nothing is
    persisted; the whole ``fulfill()`` call is rolled back).
    """

    def __init__(self, result: FulfillmentResult):
        self.result = result
        super().__init__(f"Requested {result.requested}, only {result.granted} available (guid={result.guid})")


@dataclass
class WalletSpec:
    """
    Describes one wallet to open as part of ``plan_switch`` (README: ``open_wallets: list[WalletSpec]``).
    The subject is *not* part of the spec — ``plan_switch`` opens one wallet per (subject, spec) pair
    for every subject passed in. See DECISIONS.md.
    """

    wallet_type: str
    currency: str
    balance: Decimal = Decimal("0")
    overdraft_limit: Decimal | None = None
    reset_config: dict | None = None
    valid_until: datetime | None = None


@dataclass
class _Draw:
    wallet: Wallet
    cost: Decimal
    qty: Decimal


# ---------------------------------------------------------------------------------------------
# Rounding (README: "Modifier" — "Rounding (floor/ceil/round/None) is defined per currency.")
# ---------------------------------------------------------------------------------------------

_ROUND_MODES = {
    "floor": ROUND_FLOOR,
    "ceil": ROUND_CEILING,
    "round": ROUND_HALF_UP,
}


def apply_round(value: Decimal, round_fn: str | None) -> Decimal:
    """Round ``value`` to a whole unit per a currency's ``round_fn``. ``None`` means no rounding."""
    if round_fn is None:
        return value
    try:
        mode = _ROUND_MODES[round_fn]
    except KeyError:
        raise ValueError(f"Unknown round_fn: {round_fn!r}") from None
    return value.to_integral_value(rounding=mode)


# ---------------------------------------------------------------------------------------------
# Sliding-window balance computation (README: "Wallet" — "The effective balance of a
# sliding-window wallet is computed on-the-fly from its transactions within the current window.")
# ---------------------------------------------------------------------------------------------


def _parse_time_of_day(value: str) -> tuple[int, int, int]:
    parts = (value.split(":") + ["0", "0"])[:3]
    hour, minute, second = (int(p) for p in parts)
    return hour, minute, second


def _as_utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=dt_timezone.utc)
    return at.astimezone(dt_timezone.utc)


def _last_day_clamped(year: int, month: int, day: int) -> int:
    return min(day, calendar.monthrange(year, month)[1])


def window_start(reset_config: dict, at: datetime) -> datetime:
    """
    Compute the start of the current reset window for a given ``reset_config`` and instant.

    Interprets the README "Reset config options" table. Since the README never spells out the
    concrete JSON keys, this library defines them (see DECISIONS.md):

    - Monthly: ``{"day_of_month": 1-31, "time": "HH:MM:SS"}``
    - Weekly: ``{"day_of_week": 0-6, "time": "HH:MM:SS"}`` (0 = Monday, per ``date.weekday()``)
    - Daily: ``{"time": "HH:MM:SS"}``
    - Rolling window: ``{"window_seconds": <int>}`` (default 86400 if omitted) — the README's
      "None" row in the reset-config table, i.e. no calendar anchor at all.

    All computation happens in UTC (README: "All timestamps are stored in UTC.").
    """
    at = _as_utc(at)

    if "day_of_month" in reset_config:
        hour, minute, second = _parse_time_of_day(reset_config.get("time", "00:00:00"))
        day = reset_config["day_of_month"]
        year, month = at.year, at.month
        candidate = datetime(
            year, month, _last_day_clamped(year, month, day), hour, minute, second, tzinfo=dt_timezone.utc
        )
        if candidate > at:
            month -= 1
            if month == 0:
                month = 12
                year -= 1
            candidate = datetime(
                year, month, _last_day_clamped(year, month, day), hour, minute, second, tzinfo=dt_timezone.utc
            )
        return candidate

    if "day_of_week" in reset_config:
        hour, minute, second = _parse_time_of_day(reset_config.get("time", "00:00:00"))
        target_weekday = reset_config["day_of_week"]
        days_diff = (at.weekday() - target_weekday) % 7
        candidate = (at - timedelta(days=days_diff)).replace(hour=hour, minute=minute, second=second, microsecond=0)
        if candidate > at:
            candidate -= timedelta(days=7)
        return candidate

    if "time" in reset_config:
        hour, minute, second = _parse_time_of_day(reset_config["time"])
        candidate = at.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if candidate > at:
            candidate -= timedelta(days=1)
        return candidate

    seconds = reset_config.get("window_seconds", 86400)
    return at - timedelta(seconds=seconds)


def effective_balance(wallet: Wallet, at: datetime | None = None) -> Decimal:
    """
    The balance to use for fulfillment decisions right now.

    Non-sliding-window wallets (``reset_config`` is falsy): ``wallet.balance`` is authoritative —
    return it directly.

    Sliding-window wallets: ``wallet.balance`` is the per-window allotment (a checkpoint that
    ``fulfill``/``unfulfill`` never mutate); the effective balance is that allotment plus the sum
    of every transaction recorded within the current window (consumption transactions are
    negative, so this naturally nets down as the window fills up, then "resets" as old
    transactions age out of the window). See DECISIONS.md.
    """
    at = at or timezone.now()
    if not wallet.reset_config:
        return wallet.balance
    start = window_start(wallet.reset_config, at)
    total = wallet.transactions.filter(created_at__gte=start, created_at__lte=at).aggregate(total=Sum("amount"))[
        "total"
    ] or Decimal("0")
    return wallet.balance + total


# ---------------------------------------------------------------------------------------------
# Fulfillment-order walk
# ---------------------------------------------------------------------------------------------


def _validity_q(at: datetime) -> Q:
    return (Q(valid_from__isnull=True) | Q(valid_from__lte=at)) & (Q(valid_until__isnull=True) | Q(valid_until__gte=at))


def _candidate_wallets(wallet_type, currency_names, subject_obj, at, for_update):
    ct = ContentType.objects.get_for_model(subject_obj)
    qs = (
        Wallet.objects.filter(
            wallet_type=wallet_type,
            currency__in=currency_names,
            subject_content_type=ct,
            subject_object_id=subject_obj.pk,
        )
        .filter(_validity_q(at))
        .order_by("created_at", "id")
    )
    if for_update:
        qs = qs.select_for_update()
    return qs


def _section_for(feature_def: FeatureDefinition, wallet_type: str):
    return feature_def.grant if wallet_type == WalletType.GRANT else feature_def.consumable


def _build_ordered_wallets(feature_def: FeatureDefinition, subjects: dict, at: datetime, for_update: bool) -> list:
    """
    Expand ``feature_def.fulfillment_order`` into a concrete, deduplicated, priority-ordered list
    of candidate ``Wallet`` instances (README: "Fulfillment Order").
    """
    if callable(feature_def.fulfillment_order):
        wallet_types = [wt for wt in (WalletType.GRANT, WalletType.CONSUMABLE) if _section_for(feature_def, wt)]
        currency_names = set()
        for wt in wallet_types:
            currency_names.update(_section_for(feature_def, wt).currency_names)
        if not subjects or not wallet_types:
            return []
        subject_q = Q()
        for subj in subjects.values():
            ct = ContentType.objects.get_for_model(subj)
            subject_q |= Q(subject_content_type=ct, subject_object_id=subj.pk)
        qs = (
            Wallet.objects.filter(wallet_type__in=wallet_types, currency__in=currency_names)
            .filter(subject_q)
            .filter(_validity_q(at))
        )
        if for_update:
            qs = qs.select_for_update()
        return list(feature_def.fulfillment_order(qs))

    seen_ids = set()
    ordered = []
    for step_type, step_subject in feature_def.fulfillment_order:
        wallet_types = [step_type] if step_type is not None else [WalletType.GRANT, WalletType.CONSUMABLE]
        subject_objs = [subjects.get(step_subject)] if step_subject is not None else list(subjects.values())
        for wt in wallet_types:
            section = _section_for(feature_def, wt)
            if section is None:
                continue
            for subj in subject_objs:
                if subj is None:
                    continue
                for wallet in _candidate_wallets(wt, section.currency_names, subj, at, for_update):
                    if wallet.id not in seen_ids:
                        seen_ids.add(wallet.id)
                        ordered.append(wallet)
    return ordered


def _consume(ordered_wallets, feature_def, remaining, modifier_factor, provider, at, use_overdraft, pending):
    """
    Walk ``ordered_wallets`` once, planning (but not persisting) draws to cover ``remaining``
    units of the feature's request unit. ``pending`` accumulates in-flight (not-yet-persisted)
    consumption per wallet id, so a second call (the overdraft pass) sees the first pass's draws.
    """
    drawn = []
    for wallet in ordered_wallets:
        if remaining <= 0:
            break
        section = _section_for(feature_def, wallet.wallet_type)
        rate = section.rate_for(wallet.currency) if section else None
        if rate is None:
            continue

        currency_def = provider.get_currency(wallet.currency)
        already = pending.get(wallet.id, Decimal("0"))
        current_balance = effective_balance(wallet, at) - already

        if use_overdraft:
            if wallet.overdraft_limit is None:
                continue
            capacity = max(current_balance, Decimal("0")) + wallet.overdraft_limit
        else:
            capacity = max(current_balance, Decimal("0"))

        if capacity <= 0:
            continue

        effective_rate = rate * modifier_factor
        if effective_rate == 0:
            qty_from_wallet = remaining
        else:
            qty_from_wallet = min(remaining, capacity / effective_rate)
        if qty_from_wallet <= 0:
            continue

        raw_cost = qty_from_wallet * effective_rate
        rounded_cost = apply_round(raw_cost, currency_def.round_fn)
        if rounded_cost <= 0:
            continue
        if rounded_cost > capacity:
            # Rounding pushed the cost past what's actually available — clamp to capacity rather
            # than overdraw. See DECISIONS.md for the rounding-reconciliation tradeoff this implies.
            rounded_cost = capacity

        qty_covered = rounded_cost / effective_rate if effective_rate != 0 else qty_from_wallet
        if qty_covered > remaining:
            qty_covered = remaining

        drawn.append(_Draw(wallet=wallet, cost=rounded_cost, qty=qty_covered))
        pending[wallet.id] = already + rounded_cost
        remaining -= qty_covered

    return drawn, remaining


def _max_modifier_factor(feature: str, subjects: dict, at: datetime) -> Decimal:
    """README: "Modifier" — "Where multiple modifiers apply, the most favourable (max) is used."""
    if not subjects:
        return Decimal("1")
    subject_q = Q()
    for subj in subjects.values():
        ct = ContentType.objects.get_for_model(subj)
        subject_q |= Q(subject_content_type=ct, subject_object_id=subj.pk)
    factors = list(
        LicenseModifier.objects.filter(feature=feature)
        .filter(subject_q)
        .filter(_validity_q(at))
        .values_list("factor", flat=True)
    )
    if not factors:
        return Decimal("1")
    return max(factors)


def _plan(feature_def, feature, quantity, subjects, provider, at, for_update):
    pending: dict = {}
    ordered = _build_ordered_wallets(feature_def, subjects, at, for_update)
    modifier_factor = _max_modifier_factor(feature, subjects, at)

    remaining = quantity
    drawn, remaining = _consume(ordered, feature_def, remaining, modifier_factor, provider, at, False, pending)
    if remaining > 0 and feature_def.overdraft:
        drawn2, remaining = _consume(ordered, feature_def, remaining, modifier_factor, provider, at, True, pending)
        drawn = drawn + drawn2

    return drawn, remaining, modifier_factor


# ---------------------------------------------------------------------------------------------
# Archival (README: "Wallet" — "Fully consumed wallets are archived rather than deleted.")
# ---------------------------------------------------------------------------------------------


def _archive_wallet(wallet: Wallet) -> WalletArchive:
    archive = WalletArchive.objects.create(
        original_wallet_id=wallet.id,
        subject_content_type=wallet.subject_content_type,
        subject_object_id=wallet.subject_object_id,
        wallet_type=wallet.wallet_type,
        currency=wallet.currency,
        final_balance=wallet.balance,
        valid_from=wallet.valid_from,
        valid_until=wallet.valid_until,
        overdraft_limit=wallet.overdraft_limit,
        first_overdraft_at=wallet.first_overdraft_at,
        reset_config=wallet.reset_config,
        original_created_at=wallet.created_at,
    )

    transactions = list(wallet.transactions.all().order_by("created_at", "id"))
    id_map = {}
    for txn in transactions:
        txn_archive = TransactionArchive.objects.create(
            original_transaction_id=txn.id,
            wallet_archive=archive,
            original_wallet_id=wallet.id,
            fulfillment_record=txn.fulfillment_record,
            kind=txn.kind,
            amount=txn.amount,
            feature=txn.feature,
            object_content_type=txn.object_content_type,
            object_id=txn.object_id,
            original_created_at=txn.created_at,
        )
        id_map[txn.id] = txn_archive

    # Second pass: re-link storno_of within the archived batch now that archive rows exist.
    for txn in transactions:
        if txn.storno_of_id and txn.storno_of_id in id_map:
            archived = id_map[txn.id]
            archived.storno_of = id_map[txn.storno_of_id]
            archived.save(update_fields=["storno_of"])

    # Transaction.storno_of is on_delete=PROTECT, and Django enforces PROTECT even when the
    # referencing row is *also* part of the same delete — wallet.delete()'s cascade would raise
    # ProtectedError if any storno transaction is still pointing at its original. Delete storno
    # rows (the referencing side) before their originals so nothing is left pointing at anything
    # by the time wallet.delete() cascades over what remains.
    storno_ids = [txn.pk for txn in transactions if txn.storno_of_id]
    if storno_ids:
        Transaction.objects.filter(pk__in=storno_ids).delete()
    wallet.delete()
    return archive


def _archive_if_drained(wallet: Wallet) -> bool:
    """
    Trigger condition for "fully consumed" (README doesn't define this precisely — see
    DECISIONS.md): a non-sliding-window wallet whose balance has reached zero (or below) and which
    has no overdraft capacity left to draw from again is archived immediately after being drawn
    from. Sliding-window wallets are never archived by this trigger — they replenish every window
    by design.
    """
    if wallet.reset_config:
        return False
    if wallet.balance <= 0 and wallet.overdraft_limit is None:
        _archive_wallet(wallet)
        return True
    return False


def archive_expired_wallets(at: datetime | None = None) -> int:
    """
    Maintenance entry point: archive wallets whose ``valid_until`` has passed.

    Expired wallets are excluded from fulfillment-order candidate queries (``_validity_q``), so
    they would never be visited by ``_archive_if_drained`` even if never touched again — a host
    application is expected to call this periodically (e.g. from a Celery beat task). Returns the
    number of wallets archived.
    """
    at = at or timezone.now()
    archived = 0
    with db_transaction.atomic():
        for wallet in list(Wallet.objects.select_for_update().filter(valid_until__isnull=False, valid_until__lte=at)):
            _archive_wallet(wallet)
            archived += 1
    return archived


# ---------------------------------------------------------------------------------------------
# Core API (README: "Core API")
# ---------------------------------------------------------------------------------------------


def peek(
    feature: str, quantity, subjects: dict, provider: DefinitionProvider | None = None, at=None
) -> FulfillmentResult:
    """Check availability without consuming — no writes of any kind."""
    provider = provider or get_default_provider()
    feature_def = provider.get_feature(feature)
    at = at or timezone.now()
    quantity = Decimal(str(quantity))
    subjects = subjects or {}

    drawn, remaining, modifier_factor = _plan(feature_def, feature, quantity, subjects, provider, at, for_update=False)

    granted = quantity - remaining
    return FulfillmentResult(
        requested=quantity,
        granted=granted,
        satisfied=remaining <= 0,
        transactions=[],
        guid=None,
        modifier_applied=modifier_factor if modifier_factor != Decimal("1") else None,
    )


def fulfill(
    feature: str,
    quantity,
    subjects: dict,
    guid=None,
    allow_partial: bool = False,
    provider: DefinitionProvider | None = None,
    at=None,
) -> FulfillmentResult:
    """Consume licence. See README "Core API" and DECISIONS.md for the exact semantics chosen."""
    provider = provider or get_default_provider()
    feature_def = provider.get_feature(feature)
    at = at or timezone.now()
    quantity = Decimal(str(quantity))
    subjects = subjects or {}

    with db_transaction.atomic():
        if guid is not None:
            record, _ = FulfillmentRecord.objects.get_or_create(guid=guid)
        else:
            record = FulfillmentRecord.objects.create()

        drawn, remaining, modifier_factor = _plan(
            feature_def, feature, quantity, subjects, provider, at, for_update=True
        )
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

        created = []
        for draw in drawn:
            wallet = draw.wallet
            txn = Transaction.objects.create(
                wallet=wallet,
                fulfillment_record=record,
                kind=TransactionKind.CONSUMPTION,
                amount=-draw.cost,
                feature=feature,
            )
            created.append(txn)

            if not wallet.reset_config:
                wallet.balance = wallet.balance - draw.cost
                wallet.mark_overdraft_if_needed(when=at)
            elif effective_balance(wallet, at) < 0 and wallet.first_overdraft_at is None:
                wallet.first_overdraft_at = at
            wallet.save()

            _archive_if_drained(wallet)

        return FulfillmentResult(
            requested=quantity,
            granted=granted,
            satisfied=satisfied,
            transactions=created,
            guid=record.guid,
            modifier_applied=modifier_factor if modifier_factor != Decimal("1") else None,
        )


def attach(guid, obj) -> None:
    """
    Attach a domain object to every not-yet-attached transaction under ``guid``, FIFO by
    timestamp (README: "FulfillmentRecord" — "written in two stages").
    """
    record = FulfillmentRecord.objects.get(guid=guid)
    ct = ContentType.objects.get_for_model(obj)
    transactions = list(record.transactions.filter(object_id__isnull=True).order_by("created_at", "id"))
    for txn in transactions:
        txn.object_content_type = ct
        txn.object_id = obj.pk
    if transactions:
        Transaction.objects.bulk_update(transactions, ["object_content_type", "object_id"])


def unfulfill(guid, at=None) -> None:
    """
    Reverse every not-yet-stornoed transaction under ``guid`` (README: "Reversals are recorded as
    explicit storno transactions... preventing double-storno."). Idempotent: calling it again once
    everything is already reversed is a no-op, never an error.
    """
    at = at or timezone.now()
    with db_transaction.atomic():
        record = FulfillmentRecord.objects.select_for_update().get(guid=guid)

        # A plain `.filter(storno_transactions__isnull=True)` here would need an outer join, and
        # `SELECT ... FOR UPDATE` cannot be applied across one (Postgres restriction) — use a
        # subquery exclusion instead, which keeps the locking query join-free.
        already_stornoed_ids = Transaction.objects.filter(storno_of__isnull=False).values("storno_of_id")
        originals = list(
            record.transactions.exclude(kind=TransactionKind.STORNO)
            .exclude(pk__in=already_stornoed_ids)
            .select_for_update()
            .order_by("created_at", "id")
        )
        for original in originals:
            storno_amount = -original.amount
            Transaction.objects.create(
                wallet=original.wallet,
                fulfillment_record=record,
                kind=TransactionKind.STORNO,
                amount=storno_amount,
                feature=original.feature,
                storno_of=original,
            )
            wallet = original.wallet
            if not wallet.reset_config:
                wallet.balance = wallet.balance + storno_amount
                wallet.save()


def plan_switch(subjects: dict, open_wallets: list, effective_date=None) -> None:
    """
    Atomically close all currently valid wallets for the given subjects and open new ones (README:
    "Core API"). Immediate switch when ``effective_date`` is ``None``. See DECISIONS.md for the
    ``subjects`` × ``open_wallets`` fan-out semantics.
    """
    effective_date = effective_date or timezone.now()
    with db_transaction.atomic():
        for subj in subjects.values():
            ct = ContentType.objects.get_for_model(subj)
            (
                Wallet.objects.select_for_update()
                .filter(subject_content_type=ct, subject_object_id=subj.pk)
                .filter(_validity_q(effective_date))
                .update(valid_until=effective_date)
            )
            for spec in open_wallets:
                Wallet.objects.create(
                    subject=subj,
                    wallet_type=spec.wallet_type,
                    currency=spec.currency,
                    balance=spec.balance,
                    overdraft_limit=spec.overdraft_limit,
                    reset_config=spec.reset_config,
                    valid_from=effective_date,
                    valid_until=spec.valid_until,
                )


def find_unattached_fulfillment_records(older_than=None):
    """
    README: "A background job may monitor FulfillmentRecords with unattached transactions to
    trigger recovery flows." This is the query a host application's background job would use.
    """
    qs = FulfillmentRecord.objects.filter(transactions__object_id__isnull=True).distinct()
    if older_than is not None:
        qs = qs.filter(created_at__lte=older_than)
    return qs
