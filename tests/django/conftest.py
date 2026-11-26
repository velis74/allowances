"""
``dj``: the Django API of one flavour, called from synchronous test code, once per flavour.

- ``sync``: :mod:`allowances_django.sync.shortcuts`, :mod:`allowances_django.sync.permissions` and the synchronous
  checks of ``AllowancesBackend`` (``has_perm`` …), on the synchronous store. The test runs in a test transaction.
- ``async``: :mod:`allowances_django.shortcuts`, :mod:`allowances_django.permissions` and the asynchronous checks
  (``ahas_perm`` …), each call run with ``async_to_sync``, on the asynchronous store. The asynchronous store runs a
  unit of work on a connection of its own, so the test commits (``transaction=True``).

The flavour's parameter carries the ``django_db`` marker; a test that takes ``dj`` has no ``django_db`` marker of its
own.
"""

from types import SimpleNamespace

import pytest

from asgiref.sync import async_to_sync

import allowances
import allowances.sync

from allowances.sync import driver

SHORTCUTS = [
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
    "groups_of",
    "open_wallet",
    "peek",
    "plan_switch",
    "remove_member",
    "transactions_for_object",
    "transfer",
    "unfulfill",
]
PERMISSIONS = ["grant_permission", "revoke_permission"]
ENGINE = [*SHORTCUTS, "effective_balance"]


async def _awaited(coroutine):
    return await coroutine


class Flavour:
    """
    The shortcuts and the permission grants as plain functions (``dj.fulfill(...)``), the engine functions of
    :mod:`allowances` / :mod:`allowances.sync` (``dj.engine.fulfill(store, ...)``), the backend's checks
    (``dj.has_perm(backend, user, perm)``) and Django's user checks (``dj.user_has_perm(user, perm)``).
    """

    def __init__(self, name):
        self.name = name
        if name == "sync":
            from allowances_django.sync import permissions, shortcuts
            from allowances_django.sync.store import DjangoStore

            def wrap(function):
                return function

            engine = allowances.sync
        else:
            from allowances_django import permissions, shortcuts
            from allowances_django.store import DjangoStore

            wrap = async_to_sync
            engine = allowances
        self.is_async = name == "async"
        self.store_class = DjangoStore
        for attribute in SHORTCUTS:
            setattr(self, attribute, wrap(getattr(shortcuts, attribute)))
        for attribute in PERMISSIONS:
            setattr(self, attribute, wrap(getattr(permissions, attribute)))
        self.engine = SimpleNamespace(**{name: wrap(getattr(engine, name)) for name in ENGINE})

    def store(self):
        """A new store of the flavour."""
        return self.store_class()

    def run(self, coroutine):
        """The result of a store method's coroutine, run as the flavour runs the engine."""
        if self.is_async:
            return async_to_sync(_awaited)(coroutine)
        return driver.run(coroutine)

    def has_perm(self, backend, user, perm, obj=None):
        if self.is_async:
            return async_to_sync(backend.ahas_perm)(user, perm, obj)
        return backend.has_perm(user, perm, obj)

    def get_all_permissions(self, backend, user, obj=None):
        if self.is_async:
            return async_to_sync(backend.aget_all_permissions)(user, obj)
        return backend.get_all_permissions(user, obj)

    def has_module_perms(self, backend, user, app_label):
        if self.is_async:
            return async_to_sync(backend.ahas_module_perms)(user, app_label)
        return backend.has_module_perms(user, app_label)

    def user_has_perm(self, user, perm, obj=None):
        if self.is_async:
            return async_to_sync(user.ahas_perm)(perm, obj)
        return user.has_perm(perm, obj)

    def user_has_module_perms(self, user, app_label):
        if self.is_async:
            return async_to_sync(user.ahas_module_perms)(app_label)
        return user.has_module_perms(app_label)

    def user_get_all_permissions(self, user):
        if self.is_async:
            return async_to_sync(user.aget_all_permissions)()
        return user.get_all_permissions()


@pytest.fixture(
    params=[
        pytest.param("sync", marks=pytest.mark.django_db),
        pytest.param("async", marks=pytest.mark.django_db(transaction=True)),
    ]
)
def dj(request):
    return Flavour(request.param)
