"""Reading feature and currency definitions out of Django settings."""

from __future__ import annotations

from django.conf import settings

from allowances.definitions import DictProvider

SETTINGS_KEY = "ALLOWANCES"


class SettingsProvider(DictProvider):
    """
    Reads ``settings.ALLOWANCES``.

    A ``config`` dict passed to the constructor is used instead of settings.
    """

    @property
    def config(self) -> dict:
        if self._config is not None:
            return self._config
        return getattr(settings, SETTINGS_KEY, {})
