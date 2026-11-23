"""
The default stores of the Django shortcuts, chosen by settings.

``ALLOWANCES_STORE`` names the store of :mod:`allowances_django.shortcuts` and :mod:`allowances_django.permissions`
(default ``"allowances_django.store.DjangoStore"``); ``ALLOWANCES_SYNC_STORE`` names the store of
:mod:`allowances_django.sync.shortcuts` and :mod:`allowances_django.sync.permissions` (default
``"allowances_django.sync.store.DjangoStore"``). Each is a dotted path to a callable or a callable; it is called
with no arguments for every shortcut call that has no ``store=`` argument. The synchronous store must never
suspend (see :mod:`allowances.sync`).

Besides :class:`allowances.store.Store`, the shortcuts use two store methods: ``resolve(ref)`` (``groups_of``)
and ``live_wallet_ids(subject=, wallet_type=, currency=, at=)`` (``revoke_permission``), both coroutines; see
:class:`allowances_django.queries.DjangoQueries`.
"""

from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string

from allowances.store import Store

STORE_SETTING = "ALLOWANCES_STORE"
DEFAULT_STORE = "allowances_django.store.DjangoStore"
SYNC_STORE_SETTING = "ALLOWANCES_SYNC_STORE"
DEFAULT_SYNC_STORE = "allowances_django.sync.store.DjangoStore"


def configured_store(setting: str, default: str) -> Store:
    """A new store from the factory in ``settings.<setting>`` (dotted path or callable), else from ``default``."""
    factory = getattr(settings, setting, None) or default
    if isinstance(factory, str):
        factory = import_string(factory)
    return factory()


def default_store(store: Store | None = None) -> Store:
    """``store``, or the store named by ``ALLOWANCES_STORE``."""
    return store if store is not None else configured_store(STORE_SETTING, DEFAULT_STORE)


def default_sync_store(store: Store | None = None) -> Store:
    """``store``, or the store named by ``ALLOWANCES_SYNC_STORE``."""
    return store if store is not None else configured_store(SYNC_STORE_SETTING, DEFAULT_SYNC_STORE)
