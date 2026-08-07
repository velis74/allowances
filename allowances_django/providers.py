"""Reading feature and currency definitions out of Django settings."""

from __future__ import annotations

from django.conf import settings

from allowances.definitions import DictProvider

SETTINGS_KEY = "ALLOWANCES"


class SettingsProvider(DictProvider):
    """
    Reads ``settings.ALLOWANCES``.

    A ``config`` dict may be passed instead, bypassing settings entirely — useful in tests, and for
    applications whose definitions are narrower than a global settings module: per-tenant
    configuration assembled at request time, or a plan fetched from a remote service and cached.
    """

    @property
    def config(self) -> dict:
        if self._config is not None:
            return self._config
        return getattr(settings, SETTINGS_KEY, {})
