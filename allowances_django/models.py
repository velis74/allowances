"""
Relational storage for the ``allowances`` core.

The engine does not import these models. ``allowances_django.store.DjangoStore`` converts rows to
the core's frozen value types and back.

**Subjects are stored as two plain strings, not a GenericForeignKey.** ``subject_type`` holds
``"app_label.modelname"`` and ``subject_id`` the primary key as text. Unlike a ``ContentType`` FK,
this keeps the core free of Django, allows UUID and slug primary keys (a ``PositiveBigIntegerField``
object id does not), and needs no FK to the shared ``django_content_type`` table: in a
schema-per-tenant deployment such an FK from a per-tenant table breaks ``TRUNCATE`` once a second
schema exists.

``select_related`` cannot follow a subject, and the database does not detect a dangling reference.
``allowances_django.subjects.resolve()`` fetches the object.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

# Ledger amounts: Decimal, 20 digits with 6 decimal places (fractional consumables such as 0.5 credits).
AMOUNT_MAX_DIGITS = 20
AMOUNT_DECIMAL_PLACES = 6

# A modifier's factor is a multiplier (0.5, 3.27), not a currency amount: smaller precision.
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
    TRANSFER = "transfer", "Transfer"


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
    the effective balance is computed from the transactions inside the current window.
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
    source = models.CharField(
        max_length=NAME_MAX_LENGTH,
        default="plan",
        help_text="Where the wallet came from. plan_switch opens and closes only wallets with source 'plan'.",
    )
    allotment = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS,
        decimal_places=AMOUNT_DECIMAL_PLACES,
        null=True,
        blank=True,
        help_text=(
            "The plan's amount for a wallet opened with an 'adjust' or 'top_up' switch policy; the next "
            "plan switch adds the difference against it. Null for every other wallet."
        ),
    )
    scheduled = models.JSONField(
        null=True,
        blank=True,
        help_text="What a plan switch with a future effective date changes on this wallet. Written by the engine.",
    )
    # A default rather than auto_now_add, so a backfill or a test can set a past value.
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["subject_type", "subject_id"]),
            models.Index(fields=["currency"]),
            models.Index(fields=["wallet_type"]),
            models.Index(fields=["valid_from", "valid_until"]),
            # Wallet lookup by subject, wallet type and currency.
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
    One signed ledger entry: negative for a consumption or the source side of a transfer, positive
    for a grant, a storno or the target side of a transfer, zero for the use of a right.

    ``object_type``/``object_id`` of a consumption are written in a second step: the transaction is
    created when the quota is consumed, and the domain object it paid for is attached when that
    operation succeeds. A consumption without an object means the operation did not complete;
    ``find_unattached_fulfillment_records`` returns its record.

    ``actor_type``/``actor_id`` name the subject on whose behalf the entry was made, or are empty.
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
    actor_type = models.CharField(
        max_length=NAME_MAX_LENGTH,
        blank=True,
        default="",
        help_text="Lowercased 'app_label.modelname' of the subject on whose behalf the entry was made.",
    )
    actor_id = models.CharField(
        max_length=SUBJECT_ID_MAX_LENGTH,
        blank=True,
        default="",
        help_text="Primary key of the actor, as text. Empty when the entry has no actor.",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["wallet"]),
            models.Index(fields=["record"]),
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["kind"]),
            # Sliding-window balances sum a wallet's entries over a time range.
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
    A wallet moved out of the live table after it expired or was drawn to zero or below.

    ``original_wallet_id`` is a plain integer, not a foreign key: the live wallet row is deleted
    when this row is created.
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
    source = models.CharField(max_length=NAME_MAX_LENGTH, default="plan")
    allotment = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMAL_PLACES, null=True, blank=True
    )
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
    An archived transaction, moved out with its wallet. ``original_wallet_id`` holds the wallet's id
    from the live table.
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
    actor_type = models.CharField(max_length=NAME_MAX_LENGTH, blank=True, default="")
    actor_id = models.CharField(max_length=SUBJECT_ID_MAX_LENGTH, blank=True, default="")
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


class Membership(models.Model):
    """
    One subject's direct membership of a group. A group is an ordinary subject; groups may be
    members of other groups. The engine rejects a membership that would close a cycle.
    """

    member_type = models.CharField(max_length=NAME_MAX_LENGTH)
    member_id = models.CharField(max_length=SUBJECT_ID_MAX_LENGTH)
    group_type = models.CharField(max_length=NAME_MAX_LENGTH)
    group_id = models.CharField(max_length=SUBJECT_ID_MAX_LENGTH)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["member_type", "member_id"]),
            models.Index(fields=["group_type", "group_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["member_type", "member_id", "group_type", "group_id"],
                name="allowances_membership_unique",
            ),
        ]

    def __str__(self):
        return f"Membership({self.member_type}:{self.member_id} in {self.group_type}:{self.group_id})"


class AllowancesLock(models.Model):
    """A named row that operations lock with ``SELECT ... FOR UPDATE`` to serialise one kind of change."""

    name = models.CharField(max_length=NAME_MAX_LENGTH, primary_key=True)

    def __str__(self):
        return f"AllowancesLock({self.name})"
