"""
Shared fixtures.

The important one is ``store``: every engine test runs twice, once against ``MemoryStore`` and once
against ``DjangoStore``. That is the point of the port — behaviour that differs between the two is
a bug in one of them, and this suite is where it shows up.

Subjects are plain :class:`~allowances.types.SubjectRef` values rather than real model instances.
No backend needs the object itself, only the reference, so the engine tests don't have to create
users. Converting instances to references is the Django adapter's job and is tested in
``tests/django/``.
"""

from decimal import Decimal

import pytest

from allowances.backends import MemoryStore
from allowances.definitions import DictProvider
from allowances.types import SubjectRef, WalletType


@pytest.fixture(
    params=[
        pytest.param("memory"),
        # The marker has to ride on the parameter rather than be requested from inside the fixture
        # body: pytest-django decides whether to set up (and roll back) a test database while
        # collecting markers, long before any fixture function runs. Asking for the `db` fixture
        # after the fact leaves the blocker disarmed and the writes land in the *development*
        # database instead — silently, and green.
        pytest.param("django", marks=pytest.mark.django_db),
    ]
)
def store(request):
    """A Store, once per backend."""
    if request.param == "memory":
        return MemoryStore()

    # Imported lazily so the memory parametrisation needs no database at all.
    from allowances_django.store import DjangoStore

    return DjangoStore()


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


def open_wallet(store, subject, *, currency="credits", balance="10", wallet_type=WalletType.CONSUMABLE, **kwargs):
    """Shorthand for the wallet setup nearly every test starts with."""
    return store.create_wallet(
        subject=subject,
        wallet_type=wallet_type,
        currency=currency,
        balance=Decimal(str(balance)),
        **kwargs,
    )
