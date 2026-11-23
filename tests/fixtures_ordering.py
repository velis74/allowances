"""
A custom ``fulfillment_order`` callable in an importable module, for tests of the dotted-string form.

The callable receives a ``list[Wallet]``, not a database query, so it works with every store.
"""


def order_wallets(wallets):
    """Order wallets by balance descending, then by id."""
    return sorted(wallets, key=lambda w: (-w.balance, w.id))
