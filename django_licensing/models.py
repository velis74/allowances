"""
DB models for django-licensing — concern 1 ("schema") of the library.

This module implements only the *state* layer described in the README ("Definition vs. State"):
what a subject currently holds and has consumed. The *definition* layer (Feature, Currency,
fulfillment_order, ...) lives in the host application's ``settings.LICENSING`` dict and is read by
a provider that is NOT part of this concern — see ``NEXT_STEPS.md``.

No business logic (peek/fulfill/attach/unfulfill/plan_switch, archival triggers, sliding-window
balance computation, overdraft enforcement) lives here yet. Only field definitions and the handful
of model-level helpers explicitly called out in the README as belonging to the model itself (e.g.
``Wallet.mark_overdraft_if_needed``, whose *call site* is a future concern).
"""

import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone

# Precision shared by every ledger-adjacent Decimal field (balances, amounts, limits). See
# DECISIONS.md #1 for why 20/6 was chosen instead of e.g. the more common 10/2.
AMOUNT_MAX_DIGITS = 20
AMOUNT_DECIMAL_PLACES = 6

# Precision for Modifier.factor — a multiplicative cost factor (e.g. 0.5, 3.27), not a currency
# amount. See DECISIONS.md #7.
FACTOR_MAX_DIGITS = 10
FACTOR_DECIMAL_PLACES = 6

# README examples ("send_email", "coolant_fill", "email_send", "credits", ...) are short slugs.
# 100 chars is generous headroom without inviting free-text abuse.
NAME_MAX_LENGTH = 100


class WalletType(models.TextChoices):
    """
    Which section of a Feature definition a wallet fulfils.

    The README's ``fulfillment_order`` examples (``("grant", "device")``, ``("grant", None)``,
    bare ``"location"``) walk wallets by (type, subject) pairs, which means a wallet must record
    which of the two feature sections — ``grant`` or ``consumable`` — it belongs to. See
    DECISIONS.md #3.
    """

    GRANT = "grant", "Grant"
    CONSUMABLE = "consumable", "Consumable"


class TransactionKind(models.TextChoices):
    """The three transaction kinds the README's "Transaction" section names explicitly."""

    GRANT = "grant", "Grant"
    CONSUMPTION = "consumption", "Consumption"
    STORNO = "storno", "Storno"


class SubjectMixin(models.Model):
    """
    Shared GenericForeignKey plumbing for models that reference a "Subject" (README: "Any Django
    model instance that can hold a licence... referenced via GenericForeignKey").
    """

    subject_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        related_name="+",
        help_text="Content type of the subject holding this record (a User, an Organisation, a Device, ...).",
    )
    subject_object_id = models.PositiveBigIntegerField(help_text="Primary key of the subject instance.")
    subject = GenericForeignKey("subject_content_type", "subject_object_id")

    class Meta:
        abstract = True


class Wallet(SubjectMixin, models.Model):
    """
    A subject's holding of a particular currency (README: "Wallet").

    The *effective* balance of a sliding-window wallet (``reset_config`` set) is computed on the
    fly from its transactions within the current window by the fulfillment engine — this concern
    only stores the raw ``balance`` field and the reset configuration; the on-the-fly computation
    is a future concern (see NEXT_STEPS.md).
    """

    wallet_type = models.CharField(
        max_length=16,
        choices=WalletType.choices,
        help_text="Whether this wallet fulfils a feature's 'grant' or 'consumable' section.",
    )
    currency = models.CharField(
        max_length=NAME_MAX_LENGTH,
        help_text=(
            "Name of a currency defined in settings.LICENSING['currencies']. Intentionally a plain "
            "CharField, not a FK to a DB model — currencies are 'definition', not 'state'. See "
            "DECISIONS.md #2."
        ),
    )
    balance = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS,
        decimal_places=AMOUNT_DECIMAL_PLACES,
        default=0,
        help_text="Current stored balance. For sliding-window wallets this is a checkpoint, not the effective balance.",
    )
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    overdraft_limit = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS,
        decimal_places=AMOUNT_DECIMAL_PLACES,
        null=True,
        blank=True,
        help_text="Maximum negative balance allowed for this wallet, if overdraft is permitted at all.",
    )
    first_overdraft_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Set automatically the first time balance goes negative; used by the fulfillment engine "
            "to enforce max_overdraft_duration. Set via mark_overdraft_if_needed() — the engine that "
            "calls it is a future concern."
        ),
    )
    reset_config = models.JSONField(
        null=True,
        blank=True,
        help_text=(
            "Free-form sliding-window / calendar reset configuration, per the README 'Reset config "
            "options' table (None=rolling window, time=daily, day_of_week+time=weekly, "
            "day_of_month+time=monthly). Interpreted by the fulfillment engine, not validated here."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["subject_content_type", "subject_object_id"]),
            models.Index(fields=["currency"]),
            models.Index(fields=["wallet_type"]),
            models.Index(fields=["valid_from", "valid_until"]),
        ]

    def __str__(self):
        return f"Wallet({self.wallet_type}, {self.currency}, subject={self.subject!r}, balance={self.balance})"

    def mark_overdraft_if_needed(self, when=None):
        """
        Set ``first_overdraft_at`` the first time this wallet's balance is observed to be negative.

        This is a model-level helper only — nothing in this concern calls it. The fulfillment
        engine (next concern) is responsible for calling it at the right point in the consumption
        flow and persisting the change.
        """
        if self.balance < 0 and self.first_overdraft_at is None:
            self.first_overdraft_at = when or timezone.now()
            return True
        return False


class FulfillmentRecord(models.Model):
    """
    Groups all transactions belonging to a single logical business operation (README:
    "FulfillmentRecord"), identified by a UUID ``guid`` that is generated automatically if the
    caller does not supply one, and may otherwise be supplied to group multiple fulfill() calls.
    """

    guid = models.UUIDField(default=uuid.uuid4, unique=True, editable=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"FulfillmentRecord({self.guid})"


class Transaction(models.Model):
    """
    A single ledger entry against a Wallet (README: "Transaction") — a grant, a consumption, or a
    storno (reversal) of an earlier transaction.

    The optional ``object_ct``/``object_id`` pair is the "domain object" reference the README
    describes as "written in two stages": the transaction is created at fulfillment time (this
    pair left null), and the domain object is attached once the business operation completes (a
    future concern's ``attach()`` call sets it).
    """

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="transactions")
    fulfillment_record = models.ForeignKey(
        FulfillmentRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
        help_text=(
            "Nullable so ad-hoc/manual ledger entries (e.g. an admin correction) do not require a "
            "synthetic FulfillmentRecord. Transactions created via fulfill() always have one. See "
            "DECISIONS.md #5."
        ),
    )
    kind = models.CharField(max_length=16, choices=TransactionKind.choices)
    amount = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS,
        decimal_places=AMOUNT_DECIMAL_PLACES,
        help_text="Signed change to wallet.balance caused by this transaction (negative for consumption).",
    )
    feature = models.CharField(
        max_length=NAME_MAX_LENGTH,
        blank=True,
        help_text="Name of the feature (settings.LICENSING['features']) this transaction fulfilled, for audit.",
    )
    storno_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="storno_transactions",
        help_text="The original transaction this one reverses. A transaction may be storno'd at most once.",
    )
    object_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Content type of the attached domain object (set by attach(), a future concern).",
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    attached_object = GenericForeignKey("object_content_type", "object_id")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["wallet"]),
            models.Index(fields=["fulfillment_record"]),
            models.Index(fields=["object_content_type", "object_id"]),
            models.Index(fields=["kind"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["storno_of"],
                condition=models.Q(storno_of__isnull=False),
                name="dl_transaction_storno_of_unique",
            ),
        ]

    def __str__(self):
        return f"Transaction({self.kind}, amount={self.amount}, wallet_id={self.wallet_id})"


class LicenseModifier(SubjectMixin, models.Model):
    """
    A time-limited, subject-scoped factor that adjusts the cost of consuming a specific feature
    (README: "Modifier"). Where multiple modifiers apply, the fulfillment engine picks the most
    favourable (max) — that selection logic is a future concern, this is only storage.
    """

    feature = models.CharField(max_length=NAME_MAX_LENGTH)
    factor = models.DecimalField(max_digits=FACTOR_MAX_DIGITS, decimal_places=FACTOR_DECIMAL_PLACES)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["subject_content_type", "subject_object_id"]),
            models.Index(fields=["feature"]),
            models.Index(fields=["valid_from", "valid_until"]),
        ]

    def __str__(self):
        return f"LicenseModifier({self.feature}, factor={self.factor}, subject={self.subject!r})"


class WalletArchive(models.Model):
    """
    A fully consumed wallet, moved out of the live `Wallet` table (README: "Fully consumed wallets
    are archived rather than deleted").

    Deliberately NOT a FK to `Wallet` — by the time a wallet is archived it is expected to be
    deleted from the live table, so a real FK would either dangle or force on_delete=CASCADE
    bookkeeping that defeats the point of a separate archive. `original_wallet_id` is a plain
    (non-enforced) reference instead, same as an audit/log table. See DECISIONS.md #6.

    The subject GenericForeignKey is kept "live" (real FK to ContentType) since the subject itself
    (a User, a Device, ...) is not part of what gets archived and normally still exists.
    """

    original_wallet_id = models.PositiveBigIntegerField()
    subject_content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    subject_object_id = models.PositiveBigIntegerField()
    subject = GenericForeignKey("subject_content_type", "subject_object_id")

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

    original_created_at = models.DateTimeField(help_text="wallet.created_at at the time of archival.")
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["subject_content_type", "subject_object_id"]),
            models.Index(fields=["original_wallet_id"]),
            models.Index(fields=["currency"]),
        ]

    def __str__(self):
        return f"WalletArchive(original_wallet_id={self.original_wallet_id}, currency={self.currency})"


class TransactionArchive(models.Model):
    """
    An archived `Transaction`, moved out of the live table alongside its wallet (see
    `WalletArchive`). References `WalletArchive` (not the live `Wallet`) since both rows are
    written together at archival time; `original_wallet_id` is kept too so the link survives even
    when a transaction is archived without (yet) archiving its wallet. See DECISIONS.md #6.
    """

    original_transaction_id = models.PositiveBigIntegerField()
    wallet_archive = models.ForeignKey(
        WalletArchive, on_delete=models.CASCADE, null=True, blank=True, related_name="transactions"
    )
    original_wallet_id = models.PositiveBigIntegerField()
    fulfillment_record = models.ForeignKey(
        FulfillmentRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name="archived_transactions"
    )
    kind = models.CharField(max_length=16, choices=TransactionKind.choices)
    amount = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMAL_PLACES)
    feature = models.CharField(max_length=NAME_MAX_LENGTH, blank=True)
    storno_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="storno_transactions"
    )
    object_content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    attached_object = GenericForeignKey("object_content_type", "object_id")

    original_created_at = models.DateTimeField(help_text="transaction.created_at at the time of archival.")
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["wallet_archive"]),
            models.Index(fields=["original_wallet_id"]),
            models.Index(fields=["fulfillment_record"]),
            models.Index(fields=["kind"]),
        ]

    def __str__(self):
        return f"TransactionArchive(original_transaction_id={self.original_transaction_id}, amount={self.amount})"
