"""
Django permissions held as grant wallets.

A Django permission ``"app_label.codename"`` is the feature and the currency
``"perm:app_label.codename"``. A subject holds the permission while it has a live grant wallet in
that currency with a positive balance; :func:`grant_permission` opens one and
:func:`revoke_permission` closes it (coroutines; :mod:`allowances_django.sync.permissions` has both as
synchronous functions). Members of a group hold the group's permissions.
:func:`effective_permissions` picks the permissions out of a subject's
:func:`~allowances.engine.effective_allowances`.

:class:`AllowancesBackend` answers Django's permission checks from those wallets. It has no
object-level permissions and no deny rules.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable

from asgiref.sync import sync_to_async
from django.utils import timezone

from allowances import engine
from allowances.definitions import (
    CurrencyDefinition,
    CurrencySpec,
    DefinitionProvider,
    FeatureDefinition,
    FeatureSection,
)
from allowances.errors import DefinitionNotFoundError
from allowances.store import Store
from allowances.sync.driver import run
from allowances.types import Allowance, Wallet, WalletSpec, WalletType
from allowances_django.config import default_store, default_sync_store
from allowances_django.providers import SettingsProvider
from allowances_django.subjects import subject_ref

#: Prefix of the feature and currency names that stand for Django permissions.
PERMISSION_PREFIX = "perm:"

#: ``Wallet.source`` of the wallets :func:`grant_permission` opens.
PERMISSION_SOURCE = "permission"

_CACHE_ATTRIBUTE = "_allowances_perm_cache"


def permission_feature(perm: str) -> str:
    """The feature (and currency) name of a Django permission: ``"perm:app_label.codename"``."""
    return PERMISSION_PREFIX + perm


class PermissionProvider(DefinitionProvider):
    """
    Delegates to ``base`` (default :class:`~allowances_django.providers.SettingsProvider`) and
    serves a definition for every ``perm:`` feature or currency that ``base`` does not define.

    A served feature has one grant section accepting its own currency at rate 0 and the
    fulfilment order ``[("grant", None, None)]`` (any subject, any source), so a wallet of any
    supplied subject or its groups holds the right. A served currency has no conversion and default
    rounding.
    """

    def __init__(self, base: DefinitionProvider | None = None):
        self.base = base if base is not None else SettingsProvider()

    def get_feature(self, name: str) -> FeatureDefinition:
        try:
            return self.base.get_feature(name)
        except DefinitionNotFoundError:
            if not name.startswith(PERMISSION_PREFIX):
                raise
        return FeatureDefinition(
            name=name,
            grant=FeatureSection(currencies=(CurrencySpec(currency=name, rate=Decimal("0")),)),
            consumable=None,
            subjects=frozenset(),
            fulfillment_order=[(WalletType.GRANT, None, None)],
        )

    def get_currency(self, name: str) -> CurrencyDefinition:
        try:
            return self.base.get_currency(name)
        except DefinitionNotFoundError:
            if not name.startswith(PERMISSION_PREFIX):
                raise
        return CurrencyDefinition(name=name)


async def grant_permission(
    subject,
    perm: str,
    *,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    store: Store | None = None,
) -> Wallet:
    """
    Give ``subject`` (a model instance or subject reference) the permission ``perm``: a grant
    wallet in currency ``perm:<perm>`` with balance 1 and source ``"permission"``, valid from
    ``valid_from`` (default now) until ``valid_until`` (default: no end).
    """
    spec = WalletSpec(
        wallet_type=WalletType.GRANT,
        currency=permission_feature(perm),
        balance=Decimal("1"),
        valid_until=valid_until,
    )
    return await engine.open_wallet(
        default_store(store),
        subject_ref(subject),
        spec,
        source=PERMISSION_SOURCE,
        effective_date=valid_from,
    )


async def revoke_permission(subject, perm: str, *, at: datetime | None = None, store: Store | None = None) -> int:
    """
    Close, at ``at`` (default now), every live grant wallet of ``subject`` in currency
    ``perm:<perm>`` that has not ended by then. Returns how many were closed.
    """
    store = default_store(store)
    at = at or timezone.now()
    async with store.unit_of_work():
        wallet_ids = await store.live_wallet_ids(
            subject=subject_ref(subject), wallet_type=WalletType.GRANT, currency=permission_feature(perm), at=at
        )
        for wallet_id in wallet_ids:
            await engine.close_wallet(store, wallet_id, at=at)
    return len(wallet_ids)


def effective_permissions(allowances: Iterable[Allowance]) -> dict[str, list[Allowance]]:
    """
    The Django permissions among ``allowances``: each ``"app_label.codename"`` held by a grant
    allowance in currency ``perm:app_label.codename`` with a positive balance, mapped to those
    allowances in input order. Keys are sorted.
    """
    held: dict[str, list[Allowance]] = {}
    for allowance in allowances:
        wallet = allowance.wallet
        if (
            wallet.wallet_type == WalletType.GRANT
            and wallet.currency.startswith(PERMISSION_PREFIX)
            and allowance.balance > 0
        ):
            held.setdefault(wallet.currency[len(PERMISSION_PREFIX) :], []).append(allowance)
    return dict(sorted(held.items()))


class AllowancesBackend:
    """
    Authentication backend answering permission checks from permission grant wallets of the user
    and of every group the user belongs to. It authenticates nobody.

    An active superuser has every :class:`~django.contrib.auth.models.Permission`. The permissions
    are read once per user object and cached on it; the synchronous and the asynchronous checks share
    that cache.

    ``has_perm``, ``get_all_permissions`` and ``has_module_perms`` use :mod:`allowances.sync` with the store
    from :meth:`get_sync_store`. Django (5.2 and later) calls ``ahas_perm``, ``aget_all_permissions`` and
    ``ahas_module_perms`` from asynchronous code; they use the asynchronous API with the store from
    :meth:`get_store`.
    """

    def get_store(self) -> Store:
        """The store of the asynchronous checks: ``settings.ALLOWANCES_STORE``."""
        return default_store()

    def get_sync_store(self) -> Store:
        """The store of the synchronous checks: ``settings.ALLOWANCES_SYNC_STORE``. It must never suspend."""
        return default_sync_store()

    def authenticate(self, request, **credentials):
        return None

    async def aauthenticate(self, request, **credentials):
        return None

    def get_user(self, user_id):
        return None

    async def aget_user(self, user_id):
        return None

    def get_all_permissions(self, user_obj, obj=None) -> set[str]:
        if not self._checked(user_obj, obj):
            return set()
        cached = getattr(user_obj, _CACHE_ATTRIBUTE, None)
        if cached is None:
            if user_obj.is_superuser:
                cached = self._superuser_permissions()
            else:
                cached = run(self._granted(self.get_sync_store(), user_obj))
            setattr(user_obj, _CACHE_ATTRIBUTE, cached)
        return cached

    def has_perm(self, user_obj, perm, obj=None) -> bool:
        return user_obj.is_active and perm in self.get_all_permissions(user_obj, obj)

    def has_module_perms(self, user_obj, app_label) -> bool:
        return user_obj.is_active and _has_module(self.get_all_permissions(user_obj), app_label)

    async def aget_all_permissions(self, user_obj, obj=None) -> set[str]:
        if not self._checked(user_obj, obj):
            return set()
        cached = getattr(user_obj, _CACHE_ATTRIBUTE, None)
        if cached is None:
            if user_obj.is_superuser:
                cached = await sync_to_async(self._superuser_permissions)()
            else:
                cached = await self._granted(self.get_store(), user_obj)
            setattr(user_obj, _CACHE_ATTRIBUTE, cached)
        return cached

    async def ahas_perm(self, user_obj, perm, obj=None) -> bool:
        return user_obj.is_active and perm in await self.aget_all_permissions(user_obj, obj)

    async def ahas_module_perms(self, user_obj, app_label) -> bool:
        return user_obj.is_active and _has_module(await self.aget_all_permissions(user_obj), app_label)

    @staticmethod
    def _checked(user_obj, obj) -> bool:
        """Whether the user can hold permissions here: no object, active, not anonymous."""
        return obj is None and user_obj.is_active and not user_obj.is_anonymous

    @staticmethod
    def _superuser_permissions() -> set[str]:
        from django.contrib.auth.models import Permission

        return {
            f"{app_label}.{codename}"
            for app_label, codename in Permission.objects.values_list("content_type__app_label", "codename")
        }

    @staticmethod
    async def _granted(store: Store, user_obj) -> set[str]:
        allowances = await engine.effective_allowances(
            store, subject_ref(user_obj), at=timezone.now(), wallet_types=[WalletType.GRANT]
        )
        return set(effective_permissions(allowances))


def _has_module(permissions: set[str], app_label: str) -> bool:
    prefix = app_label + "."
    return any(perm.startswith(prefix) for perm in permissions)
