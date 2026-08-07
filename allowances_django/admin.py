"""
Read-mostly admin for inspecting the ledger.

Balances are meant to move through :mod:`allowances_django.shortcuts`, which writes a transaction
for every change. Editing a balance directly in the admin leaves no trace of why it moved, so the
transaction and archive tables are registered read-only — visible for support work, not editable.
"""

from django.contrib import admin

from allowances.types import SubjectRef
from allowances_django.models import (
    FulfillmentRecord,
    LicenseModifier,
    Transaction,
    TransactionArchive,
    Wallet,
    WalletArchive,
)
from allowances_django.subjects import resolve


class SubjectDisplayMixin:
    @admin.display(description="Subject")
    def subject(self, obj):
        resolved = resolve(SubjectRef(obj.subject_type, obj.subject_id))
        return resolved if resolved is not None else f"{obj.subject_type}:{obj.subject_id} (missing)"


@admin.register(Wallet)
class WalletAdmin(SubjectDisplayMixin, admin.ModelAdmin):
    list_display = ("id", "subject", "wallet_type", "currency", "balance", "valid_from", "valid_until")
    list_filter = ("wallet_type", "currency")
    search_fields = ("subject_type", "subject_id", "currency")
    date_hierarchy = "created_at"


@admin.register(FulfillmentRecord)
class FulfillmentRecordAdmin(admin.ModelAdmin):
    list_display = ("guid", "created_at")
    search_fields = ("guid",)
    date_hierarchy = "created_at"


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "wallet", "kind", "amount", "feature", "created_at")
    list_filter = ("kind", "feature")
    search_fields = ("feature", "object_type", "object_id")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(LicenseModifier)
class LicenseModifierAdmin(SubjectDisplayMixin, admin.ModelAdmin):
    list_display = ("id", "subject", "feature", "factor", "valid_from", "valid_until")
    list_filter = ("feature",)
    search_fields = ("subject_type", "subject_id", "feature")


@admin.register(WalletArchive)
class WalletArchiveAdmin(SubjectDisplayMixin, admin.ModelAdmin):
    list_display = ("id", "original_wallet_id", "subject", "currency", "final_balance", "archived_at")
    list_filter = ("wallet_type", "currency")
    search_fields = ("subject_type", "subject_id", "currency")
    date_hierarchy = "archived_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(TransactionArchive)
class TransactionArchiveAdmin(admin.ModelAdmin):
    list_display = ("id", "original_transaction_id", "original_wallet_id", "kind", "amount", "archived_at")
    list_filter = ("kind", "feature")
    date_hierarchy = "archived_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
