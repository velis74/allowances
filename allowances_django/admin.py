"""
Admin for the ledger.

Transactions, memberships and the archive tables cannot be added or changed in the admin; deletion
is allowed. Wallets, fulfilment records and licence modifiers are editable. A balance edited here
has no ledger entry; :mod:`allowances_django.shortcuts` writes a transaction for every change.
"""

from django.contrib import admin

from allowances.types import SubjectRef
from allowances_django.models import (
    FulfillmentRecord,
    LicenseModifier,
    Membership,
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
    list_display = ("id", "subject", "wallet_type", "currency", "source", "balance", "valid_from", "valid_until")
    list_filter = ("wallet_type", "currency", "source")
    search_fields = ("subject_type", "subject_id", "currency")
    date_hierarchy = "created_at"


@admin.register(FulfillmentRecord)
class FulfillmentRecordAdmin(admin.ModelAdmin):
    list_display = ("guid", "created_at")
    search_fields = ("guid",)
    date_hierarchy = "created_at"


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "wallet", "kind", "amount", "feature", "actor", "created_at")
    list_filter = ("kind", "feature")
    search_fields = ("feature", "object_type", "object_id", "actor_type", "actor_id")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Actor")
    def actor(self, obj):
        return f"{obj.actor_type}:{obj.actor_id}" if obj.actor_type else "-"


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    """No add or change: memberships change through ``add_member`` and ``remove_member``, which reject cycles."""

    list_display = ("id", "member", "group", "created_at")
    search_fields = ("member_type", "member_id", "group_type", "group_id")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Member")
    def member(self, obj):
        resolved = resolve(SubjectRef(obj.member_type, obj.member_id))
        return resolved if resolved is not None else f"{obj.member_type}:{obj.member_id}"

    @admin.display(description="Group")
    def group(self, obj):
        resolved = resolve(SubjectRef(obj.group_type, obj.group_id))
        return resolved if resolved is not None else f"{obj.group_type}:{obj.group_id}"


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
