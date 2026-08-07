"""
A custom ``fulfillment_order`` callable, kept as a real importable module because the dotted-string
form has to resolve to something.

Note the signature: it receives a plain ``list[Wallet]``, not a database query. That is what makes
a custom ordering portable — the same function works whether the wallets came out of Postgres or a
dict.
"""


def order_wallets(wallets):
    """Drain the fattest wallet first."""
    return sorted(wallets, key=lambda w: (-w.balance, w.id))
