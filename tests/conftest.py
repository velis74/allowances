"""
Shared fixtures.

``store`` runs every engine test once per store:

- ``memory``: :class:`~allowances.backends.MemoryStore`;
- ``django``: ``allowances_django.store.DjangoStore``, the asynchronous Django store;
- ``django-sync``: ``allowances_django.sync.store.DjangoStore`` driven through :mod:`allowances.sync` on a worker
  thread (:class:`tests.api.ThreadedSyncStore`);
- ``adorm-django``: ``allowances_adorm.store.AdormStore`` on adorm's Django driver;
- ``adorm-asyncpg``: the same store on adorm's native PostgreSQL driver, PostgreSQL only
  (profiles in ``tests/adorm_support.py``).

Behaviour that differs between stores is a bug in one of them. Tests call the engine through :mod:`tests.api`,
which picks the asynchronous or the synchronous API for the store.

The database stores commit (``transaction=True``): the stores run their work on connections of their own, which do
not see a test transaction of the main connection.

Subjects are plain :class:`~allowances.types.SubjectRef` values, not model instances: stores need only the
reference, so the engine tests create no users. Converting instances to references is tested in ``tests/django/``.
"""

from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from allowances.backends import MemoryStore
from allowances.definitions import DictProvider
from allowances.types import SubjectRef, WalletType
from tests.api import ThreadedSyncStore

# The django_db marker goes on the parameter: pytest-django sets up the test database from markers at collection
# time. Requesting `db` inside the fixture is too late, and writes go to the development database without an error.
COMMITTING = pytest.mark.django_db(transaction=True)

DATABASE_STORES = ["django", "django-sync", "adorm-django", "adorm-asyncpg"]
ALL_STORES = ["memory", *DATABASE_STORES]


def store_params(kinds):
    return [pytest.param(kind, marks=() if kind == "memory" else COMMITTING) for kind in kinds]


def _skip_unless_supported(kind):
    from django.db import connection

    if kind == "adorm-asyncpg" and connection.vendor != "postgresql":
        pytest.skip("adorm's native driver (asyncpg) runs on PostgreSQL only")


def new_store(kind, worker=0):
    """
    A new store of ``kind``. ``worker`` separates concurrent callers: each gets its own worker thread
    (``django-sync``) or adorm session (``adorm-*``).
    """
    if kind == "memory":
        return MemoryStore()
    if kind == "django":
        from allowances_django.store import DjangoStore

        return DjangoStore()
    if kind == "django-sync":
        from allowances_django.sync.store import DjangoStore as SyncDjangoStore

        return ThreadedSyncStore(SyncDjangoStore())
    from tests.adorm_support import adorm_store

    return adorm_store(kind, session_name=f"worker-{worker}")


@asynccontextmanager
async def store_maker(kind):
    """
    Yield a factory of stores of ``kind`` (``factory(worker=0)``); on exit, close what the stores opened on threads
    of their own and the connection of Django's asynchronous ORM.
    """
    _skip_unless_supported(kind)
    made = []

    def factory(worker=0):
        made.append(new_store(kind, worker))
        return made[-1]

    try:
        yield factory
    finally:
        for store in made:
            if isinstance(store, ThreadedSyncStore):
                store.close()
        if kind != "memory":
            from asgiref.sync import sync_to_async
            from django.db import connections

            await sync_to_async(connections.close_all)()


@pytest.fixture(params=store_params(ALL_STORES))
async def store(request):
    """A store, once per kind."""
    async with store_maker(request.param) as factory:
        yield factory()


@pytest.fixture(params=store_params(DATABASE_STORES))
async def db_store(request):
    """A store, once per kind that keeps its data in the database."""
    async with store_maker(request.param) as factory:
        yield factory()


@pytest.fixture(params=store_params(DATABASE_STORES))
async def store_factory(request):
    """``store_factory(worker)``: a new store per concurrent caller, once per kind that keeps its data in the database."""
    async with store_maker(request.param) as factory:
        yield factory


@pytest.fixture
def alice():
    return SubjectRef("auth.user", 1)


@pytest.fixture
def bob():
    return SubjectRef("auth.user", 2)


def provider_with(features, currencies):
    return DictProvider({"features": features, "currencies": currencies})


CREDITS = {"credits": {"general": True, "round_fn": None}}


@pytest.fixture
def provider():
    """The simplest useful definition: one feature, one consumable currency, one subject."""
    return provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies=CREDITS,
    )


async def open_wallet(store, subject, *, currency="credits", balance="10", wallet_type=WalletType.CONSUMABLE, **kwargs):
    """Shorthand for the wallet setup nearly every test starts with."""
    return await store.create_wallet(
        subject=subject,
        wallet_type=wallet_type,
        currency=currency,
        balance=Decimal(str(balance)),
        **kwargs,
    )
