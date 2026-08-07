"""
Relational storage for the ``allowances`` core.

These models are a persistence detail, not the library's vocabulary — the engine never imports
them and never sees an instance. ``allowances_django.store.DjangoStore`` converts rows into the
core's frozen value types on the way out and back on the way in.

**Subjects are stored as two plain strings, not a GenericForeignKey.** ``subject_type`` holds
``"app_label.modelname"`` and ``subject_id`` the primary key as text. This buys three things a
``ContentType`` FK cannot: the core stays free of Django, subjects with UUID or slug primary keys
work (a ``PositiveBigIntegerField`` object id rules those out), and a schema-per-tenant deployment
stops fighting the shared ``django_content_type`` table — a real FK from a per-tenant table to it
is legal to create but breaks ``TRUNCATE`` once a second schema exists, which is why the old schema
had to carry ``db_constraint=False`` everywhere.

The cost is that ``select_related`` cannot follow a subject and a dangling reference is not caught
by the database. ``allowances_django.subjects.resolve()`` fetches the object when you need it.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

# Ledger amounts are Decimal throughout. 20 digits with 6 decimal places covers fractional
# consumables (0.5 credits) and large balances without ever reaching for a float.
AMOUNT_MAX_DIGITS = 20
AMOUNT_DECIMAL_PLACES = 6

# A modifier's factor is a multiplier (0.5, 3.27), not a currency amount, so it gets its own,
# smaller precision.
FACTOR_MAX_DIGITS = 10
FACTOR_DECIMAL_PLACES = 6

NAME_MAX_LENGTH = 100
# Long enough for a UUID in any formatting, a numeric pk, or a slug.
SUBJECT_ID_MAX_LENGTH = 64


class WalletType(models.TextChoices):
    GRANT = "grant", "Grant"
    CONSUMABLE = "consumable", "Consumable"


class TransactionKind(models.TextChoices):
    GRANT = "grant", "Grant"
    CONSUMPTION = "consumption", "Consumption"
    STORNO = "storno", "Storno"


class SubjectFields(models.Model):
    """The two columns that identify whoever holds a record."""

    subject_type = models.CharField(
        max_length=NAME_MAX_LENGTH,
        help_text="Lowercased 'app_label.modelname' of the subject, e.g. 'auth.user'.",
    )
    subject_id = models.CharField(
        max_length=SUBJECT_ID_MAX_LENGTH,
        help_text="Primary key of the subject, as text — so integer, UUID and slug keys all work.",
    )

    class Meta:
        abstract = True


class Wallet(SubjectFields):
    """
    A subject's holding of one currency.

    For a wallet with a ``reset_config``, ``balance`` is the per-window allotment and never moves;
    the balance that governs decisions is computed from the transactions inside the current window.
    """

    wallet_type = models.CharField(max_length=16, choices=WalletType.choices)
    currency = models.CharField(
        max_length=NAME_MAX_LENGTH,
        help_text="Name of a currency declared in settings.ALLOWANCES['currencies'].",
    )
    balance = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMAL_PLACES, default=0)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    overdraft_limit = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS,
        decimal_places=AMOUNT_DECIMAL_PLACES,
        null=True,
        blank=True,
        help_text="How far below zero this wallet may go. Null means no overdraft at all.",
    )
    first_overdraft_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the balance first went negative. Set by the engine, never cleared.",
    )
    reset_config = models.JSONField(
        null=True,
        blank=True,
        help_text=(
            "Sliding-window configuration: {'time': ...} daily, {'day_of_week', 'time'} weekly, "
            "{'day_of_month', 'time'} monthly, {'window_seconds': N} rolling. Null means the "
            "balance is a plain stored amount that does not replenish."
        ),
    )
    # Not auto_now_add: the store sets it, so a backfill or a test can place a wallet in the past.
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["subject_type", "subject_id"]),
            models.Index(fields=["currency"]),
            models.Index(fields=["wallet_type"]),
            models.Index(fields=["valid_from", "valid_until"]),
            # Candidate lookups always filter on this whole combination, then order by created_at.
            models.Index(fields=["subject_type", "subject_id", "wallet_type", "currency"]),
        ]

    def __str__(self):
        return f"Wallet({self.wallet_type}, {self.currency}, {self.subject_type}:{self.subject_id}, {self.balance})"


class FulfillmentRecord(models.Model):
    """Groups every transaction belonging to one business operation."""

    guid = models.UUIDField(default=uuid.uuid4, unique=True, editable=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"FulfillmentRecord({self.guid})"


class Transaction(models.Model):
    """
    One signed ledger entry: negative for consumption, positive for a grant or a storno.

    ``object_type``/``object_id`` are written in a second step. The transaction is created when the
    quota is consumed; the domain object it paid for is attached once that operation actually
    succeeds. A transaction left unattached is therefore a usable signal that something failed in
    between.
    """

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="transactions")
    record = models.ForeignKey(
        FulfillmentRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
        help_text="Null for ad-hoc entries such as a manual admin correction, which need no record.",
    )
    kind = models.CharField(max_length=16, choices=TransactionKind.choices)
    amount = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMAL_PLACES)
    feature = models.CharField(max_length=NAME_MAX_LENGTH, blank=True)
    storno_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="storno_transactions",
        help_text="The entry this one reverses.",
    )
    object_type = models.CharField(max_length=NAME_MAX_LENGTH, blank=True)
    object_id = models.CharField(max_length=SUBJECT_ID_MAX_LENGTH, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["wallet"]),
            models.Index(fields=["record"]),
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["kind"]),
            # Sliding-window balances sum a wallet's entries over a time range on every fulfillment.
            models.Index(fields=["wallet", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["storno_of"],
                condition=models.Q(storno_of__isnull=False),
                name="allowances_transaction_storno_of_unique",
            ),
        ]

    def __str__(self):
        return f"Transaction({self.kind}, {self.amount}, wallet={self.wallet_id})"


class LicenseModifier(SubjectFields):
    """A time-limited factor adjusting what one unit of a feature costs a given subject."""

    feature = models.CharField(max_length=NAME_MAX_LENGTH)
    factor = models.DecimalField(max_digits=FACTOR_MAX_DIGITS, decimal_places=FACTOR_DECIMAL_PLACES)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["subject_type", "subject_id"]),
            models.Index(fields=["feature"]),
            models.Index(fields=["valid_from", "valid_until"]),
        ]

    def __str__(self):
        return f"LicenseModifier({self.feature}, ×{self.factor}, {self.subject_type}:{self.subject_id})"


class WalletArchive(SubjectFields):
    """
    A wallet that has been spent out or has expired, moved here so the live tables only hold
    wallets that can still pay for something.

    Deliberately not a foreign key back to :class:`Wallet` — the live row is gone by the time this
    one exists, so a real FK would either dangle or force cascade bookkeeping that defeats the
    point. ``original_wallet_id`` is a plain reference, the way an audit table's is.
    """

    original_wallet_id = models.BigIntegerField()
    wallet_type = models.CharField(max_length=16, choices=WalletType.choices)
    currency = models.CharField(max_length=NAME_MAX_LENGTH)
    final_balance = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMAL_PLACES)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    overdraft_limit = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMAL_PLACES, null=True, blank=True
    )
    first_overdraft_at = models.DateTimeField(null=True, blank=True)
    reset_config = models.JSONField(null=True, blank=True)
    original_created_at = models.DateTimeField()
    archived_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["subject_type", "subject_id"]),
            models.Index(fields=["original_wallet_id"]),
            models.Index(fields=["currency"]),
        ]

    def __str__(self):
        return f"WalletArchive(wallet={self.original_wallet_id}, {self.currency})"


class TransactionArchive(models.Model):
    """
    An archived transaction, moved out alongside its wallet. ``original_wallet_id`` is kept as well
    as the archive FK, so the link survives even if a transaction is archived without its wallet.
    """

    original_transaction_id = models.BigIntegerField()
    wallet_archive = models.ForeignKey(
        WalletArchive, on_delete=models.CASCADE, null=True, blank=True, related_name="transactions"
    )
    original_wallet_id = models.BigIntegerField()
    record = models.ForeignKey(
        FulfillmentRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name="archived_transactions"
    )
    kind = models.CharField(max_length=16, choices=TransactionKind.choices)
    amount = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMAL_PLACES)
    feature = models.CharField(max_length=NAME_MAX_LENGTH, blank=True)
    storno_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="storno_transactions"
    )
    object_type = models.CharField(max_length=NAME_MAX_LENGTH, blank=True)
    object_id = models.CharField(max_length=SUBJECT_ID_MAX_LENGTH, blank=True)
    original_created_at = models.DateTimeField()
    archived_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["wallet_archive"]),
            models.Index(fields=["original_wallet_id"]),
            models.Index(fields=["record"]),
            models.Index(fields=["kind"]),
        ]

    def __str__(self):
        return f"TransactionArchive(transaction={self.original_transaction_id}, {self.amount})"
