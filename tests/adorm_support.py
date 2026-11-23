"""
adorm profiles and stores for the test suite.

Two adorm profiles over the ``default`` alias of ``tests/settings.py``:

- ``default``: adorm's Django driver. Under ``django_db`` its slots share the test's connection; under
  ``django_db(transaction=True)`` they open connections of their own to the test database.
- ``asyncpg``: adorm's native PostgreSQL driver, configured only when the test database is PostgreSQL. It runs only
  under ``django_db(transaction=True)``: its own connections can neither see nor roll back a ``TestCase``
  transaction.

``tests/settings.py`` can set ``ADORM = adorm_settings(DATABASES["default"])`` with ``"adorm.contrib.django"`` in
``INSTALLED_APPS``; otherwise :func:`adorm_store` configures adorm itself.
"""

from __future__ import annotations

DJANGO_PROFILE = "default"
ASYNCPG_PROFILE = "asyncpg"

#: The adorm profile of each store kind of the test matrix.
PROFILES = {"adorm-django": DJANGO_PROFILE, "adorm-asyncpg": ASYNCPG_PROFILE}

POSTGRESQL_ENGINE = "django.db.backends.postgresql"


def adorm_settings(database: dict) -> dict:
    """The ``ADORM`` setting (keyword arguments of ``adorm.configure()``) for the ``default`` alias ``database``."""
    profiles = {DJANGO_PROFILE: {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"}}
    if database["ENGINE"] == POSTGRESQL_ENGINE:
        profiles[ASYNCPG_PROFILE] = {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "alias": "default"}
    return {"profiles": profiles, "pool": {"max_connections": 10, "acquire_timeout": 20}}


def on_postgresql() -> bool:
    """Whether the test database is PostgreSQL."""
    from django.conf import settings

    return settings.DATABASES["default"]["ENGINE"] == POSTGRESQL_ENGINE


def configure_adorm() -> None:
    """
    Configure adorm with :func:`adorm_settings` unless both profiles it needs are registered. Configuring with the
    same values keeps the profiles' pools.
    """
    import adorm

    from adorm.settings import get_profile
    from django.conf import settings

    config = adorm_settings(settings.DATABASES["default"])
    if all(get_profile(name) is not None for name in config["profiles"]):
        return
    adorm.configure(**config)


def adorm_store(kind: str, *, session_name: str = "default"):
    """An ``AdormStore`` of the store kind ``kind`` (``"adorm-django"`` or ``"adorm-asyncpg"``)."""
    from allowances_adorm.store import AdormStore

    configure_adorm()
    return AdormStore(PROFILES[kind], session_name=session_name)
