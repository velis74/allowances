"""
Permission grants for synchronous Django code: :func:`grant_permission` and :func:`revoke_permission` of
:mod:`allowances_django.permissions` as plain functions, on the store from ``settings.ALLOWANCES_SYNC_STORE``.
The other names are those of :mod:`allowances_django.permissions`.
"""

from __future__ import annotations

from allowances_django import permissions as _permissions
from allowances_django.permissions import (
    AllowancesBackend,
    effective_permissions,
    permission_feature,
    PERMISSION_PREFIX,
    PERMISSION_SOURCE,
    PermissionProvider,
)
from allowances_django.sync.driver import synchronous

grant_permission = synchronous(_permissions.grant_permission, __name__)
revoke_permission = synchronous(_permissions.revoke_permission, __name__)

__all__ = [
    "AllowancesBackend",
    "PERMISSION_PREFIX",
    "PERMISSION_SOURCE",
    "PermissionProvider",
    "effective_permissions",
    "grant_permission",
    "permission_feature",
    "revoke_permission",
]
