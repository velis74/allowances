from django.apps import AppConfig
from django.core.checks import register


class AllowancesConfig(AppConfig):
    name = "allowances_django"
    # Tables come out as `allowances_wallet` rather than `allowances_django_wallet`.
    label = "allowances"
    verbose_name = "Allowances"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from allowances_django.checks import check_sqlite_transaction_mode

        register(check_sqlite_transaction_mode)
