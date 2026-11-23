"""The ``allowances_django`` admin classes and the ``ALLOWANCES_ADMIN_READ_ONLY`` setting."""

import pytest

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import override_settings, RequestFactory

import allowances_django.admin  # noqa: F401  registers the ModelAdmins

from allowances_django.models import (
    FulfillmentRecord,
    LicenseModifier,
    Membership,
    Transaction,
    TransactionArchive,
    Wallet,
    WalletArchive,
)

pytestmark = pytest.mark.django_db

MODELS = [Wallet, FulfillmentRecord, Transaction, Membership, LicenseModifier, WalletArchive, TransactionArchive]
EDITABLE = [Wallet, FulfillmentRecord, LicenseModifier]


@pytest.fixture
def request_as_superuser():
    request = RequestFactory().get("/admin/")
    request.user = User.objects.create_superuser("root", "root@example.com", "x")
    return request


def model_admin(model):
    return admin.site._registry[model]


@pytest.mark.parametrize("model", EDITABLE, ids=lambda m: m.__name__)
def test_editable_models_can_be_added_changed_and_deleted_by_default(model, request_as_superuser):
    model_admin_ = model_admin(model)

    assert model_admin_.has_add_permission(request_as_superuser) is True
    assert model_admin_.has_change_permission(request_as_superuser) is True
    assert model_admin_.has_delete_permission(request_as_superuser) is True


@pytest.mark.parametrize(
    "model", [Transaction, Membership, WalletArchive, TransactionArchive], ids=lambda m: m.__name__
)
def test_ledger_models_can_only_be_deleted_by_default(model, request_as_superuser):
    model_admin_ = model_admin(model)

    assert model_admin_.has_add_permission(request_as_superuser) is False
    assert model_admin_.has_change_permission(request_as_superuser) is False
    assert model_admin_.has_delete_permission(request_as_superuser) is True


@override_settings(ALLOWANCES_ADMIN_READ_ONLY=True)
@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_read_only_admin_allows_viewing_only(model, request_as_superuser):
    model_admin_ = model_admin(model)

    assert model_admin_.has_add_permission(request_as_superuser) is False
    assert model_admin_.has_change_permission(request_as_superuser) is False
    assert model_admin_.has_delete_permission(request_as_superuser) is False
    assert model_admin_.has_view_permission(request_as_superuser) is True


@override_settings(ALLOWANCES_ADMIN_READ_ONLY=True)
def test_read_only_admin_still_needs_the_view_permission():
    request = RequestFactory().get("/admin/")
    request.user = User.objects.create_user("clerk", "clerk@example.com", "x", is_staff=True)

    assert model_admin(Wallet).has_view_permission(request) is False
