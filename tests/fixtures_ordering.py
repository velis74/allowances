"""
A trivial custom fulfillment-order callable, used to exercise the README's dotted-string callable
form of ``fulfillment_order`` ("a callable that receives a QuerySet of candidate wallets and
returns it ordered"). Kept as a real importable module (not an inline test function) since
``django.utils.module_loading.import_string`` needs a dotted path that actually resolves.
"""


def order_wallets(queryset):
    """Orders candidate wallets balance descending — drain the fattest wallet first."""
    return queryset.order_by("-balance", "id")
