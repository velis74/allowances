"""
Engine functions for synchronous Django code: the functions of :mod:`allowances_django.shortcuts` as plain
functions with the same signatures.

The default store is ``settings.ALLOWANCES_SYNC_STORE``, by default :class:`allowances_django.sync.store.DjangoStore`
(see :mod:`allowances_django.config`); a ``store=`` argument must also be a store that never suspends::

    from allowances_django.sync.shortcuts import fulfill

    result = fulfill("send_email", 1, {"user": request.user, "team": team})
"""

from __future__ import annotations

from allowances.store import Store
from allowances_django import shortcuts as _shortcuts
from allowances_django.config import default_sync_store
from allowances_django.shortcuts import get_provider
from allowances_django.sync.driver import synchronous


def get_store(store: Store | None = None) -> Store:
    """``store``, or a new store from ``settings.ALLOWANCES_SYNC_STORE``."""
    return default_sync_store(store)


add_member = synchronous(_shortcuts.add_member, __name__)
adjust_wallet = synchronous(_shortcuts.adjust_wallet, __name__)
archive_expired_wallets = synchronous(_shortcuts.archive_expired_wallets, __name__)
attach = synchronous(_shortcuts.attach, __name__)
cancel_scheduled_switch = synchronous(_shortcuts.cancel_scheduled_switch, __name__)
close_wallet = synchronous(_shortcuts.close_wallet, __name__)
consumed = synchronous(_shortcuts.consumed, __name__)
credit = synchronous(_shortcuts.credit, __name__)
effective_allowances = synchronous(_shortcuts.effective_allowances, __name__)
find_unattached_fulfillment_records = synchronous(_shortcuts.find_unattached_fulfillment_records, __name__)
fulfill = synchronous(_shortcuts.fulfill, __name__)
groups_of = synchronous(_shortcuts.groups_of, __name__)
open_wallet = synchronous(_shortcuts.open_wallet, __name__)
peek = synchronous(_shortcuts.peek, __name__)
plan_switch = synchronous(_shortcuts.plan_switch, __name__)
remove_member = synchronous(_shortcuts.remove_member, __name__)
transfer = synchronous(_shortcuts.transfer, __name__)
unfulfill = synchronous(_shortcuts.unfulfill, __name__)

__all__ = [
    "add_member",
    "adjust_wallet",
    "archive_expired_wallets",
    "attach",
    "cancel_scheduled_switch",
    "close_wallet",
    "consumed",
    "credit",
    "effective_allowances",
    "find_unattached_fulfillment_records",
    "fulfill",
    "get_provider",
    "get_store",
    "groups_of",
    "open_wallet",
    "peek",
    "plan_switch",
    "remove_member",
    "transfer",
    "unfulfill",
]
