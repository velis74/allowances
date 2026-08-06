from django.contrib import admin

from django_licensing.models import (
    FulfillmentRecord,
    LicenseModifier,
    Transaction,
    TransactionArchive,
    Wallet,
    WalletArchive,
)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("id", "wallet_type", "currency", "balance", "subject", "valid_from", "valid_until")
    list_filter = ("wallet_type", "currency")


@admin.register(WalletArchive)
class WalletArchiveAdmin(admin.ModelAdmin):
    list_display = ("id", "original_wallet_id", "wallet_type", "currency", "final_balance", "archived_at")
    list_filter = ("wallet_type", "currency")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "amount", "wallet", "fulfillment_record", "feature", "created_at")
    list_filter = ("kind", "feature")


@admin.register(TransactionArchive)
class TransactionArchiveAdmin(admin.ModelAdmin):
    list_display = ("id", "original_transaction_id", "kind", "amount", "archived_at")
    list_filter = ("kind", "feature")


@admin.register(FulfillmentRecord)
class FulfillmentRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "guid", "created_at")


@admin.register(LicenseModifier)
class LicenseModifierAdmin(admin.ModelAdmin):
    list_display = ("id", "feature", "factor", "subject", "valid_from", "valid_until")
    list_filter = ("feature",)
