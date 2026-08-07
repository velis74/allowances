from django.apps import AppConfig


class AllowancesConfig(AppConfig):
    name = "allowances_django"
    # Tables come out as `allowances_wallet` rather than `allowances_django_wallet`.
    label = "allowances"
    verbose_name = "Allowances"
    default_auto_field = "django.db.models.BigAutoField"
