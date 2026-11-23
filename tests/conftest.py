"""
Shared fixtures.

``store`` runs every engine test twice, once against ``MemoryStore`` and once against
``DjangoStore``. Behaviour that differs between the two backends is a bug in one of them.

Subjects are plain :class:`~allowances.types.SubjectRef` values, not model instances: backends
need only the reference, so the engine tests create no users. Converting instances to references
is tested in ``tests/django/``.
"""

from decimal import Decimal

import pytest

from allowances.backends import MemoryStore
from allowances.definitions import DictProvider
from allowances.types import SubjectRef, WalletType


@pytest.fixture(
    params=[
        pytest.param("memory"),
        # The django_db marker goes on the parameter: pytest-django sets up the test database from
        # markers at collection time. Requesting `db` inside the fixture is too late, and writes go
        # to the development database without an error.
        pytest.param("django", marks=pytest.mark.django_db),
    ]
)
def store(request):
    """A Store, once per backend."""
    if request.param == "memory":
        return MemoryStore()

    # Imported lazily so the memory parametrisation needs no database.
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
