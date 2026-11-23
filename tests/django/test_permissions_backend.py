"""
``allowances_django.permissions``: the permission provider, granting and revoking, and
``AllowancesBackend`` called directly and through Django's ``User.has_perm``.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from django.contrib.auth.models import AnonymousUser, Group, Permission, User
from django.test import override_settings
from django.utils import timezone

from allowances.definitions import DictProvider
from allowances.errors import DefinitionNotFoundError
from allowances.types import WalletSpec, WalletType
from allowances_django import shortcuts
from allowances_django.models import Wallet
from allowances_django.permissions import (
    AllowancesBackend,
    grant_permission,
    permission_feature,
    PERMISSION_PREFIX,
    PERMISSION_SOURCE,
    PermissionProvider,
    revoke_permission,
)

pytestmark = pytest.mark.django_db

PERM = "news.change_article"
OTHER = "news.delete_article"
BACKEND = "allowances_django.permissions.AllowancesBackend"


@pytest.fixture
def backend():
    return AllowancesBackend()


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


@pytest.fixture
def editors():
    return Group.objects.create(name="editors")


def fresh(user):
    return User.objects.get(pk=user.pk)


# -- provider -------------------------------------------------------------------------------------


def test_permission_feature_prefixes_the_permission():
    assert PERMISSION_PREFIX == "perm:"
    assert permission_feature(PERM) == "perm:news.change_article"


def test_the_provider_serves_a_rate_zero_grant_feature_for_a_permission():
    feature = PermissionProvider(DictProvider({})).get_feature(permission_feature(PERM))

    assert feature.consumable is None
    assert feature.grant.rate_for(permission_feature(PERM)) == Decimal("0")
    assert list(feature.fulfillment_order) == [(WalletType.GRANT, None, None)]
    assert feature.subjects == frozenset()


def test_the_provider_serves_a_plain_currency_for_a_permission():
    currency = PermissionProvider(DictProvider({})).get_currency(permission_feature(PERM))

    assert currency.name == permission_feature(PERM)
    assert currency.general is False
    assert currency.round_fn is None


def test_the_provider_prefers_the_base_definitions():
    name = permission_feature(PERM)
    base = DictProvider(
        {
            "features": {name: {"consumable": {"currencies": ["credits"]}, "fulfillment_order": ["user"]}},
            "currencies": {name: {"general": True}},
        }
    )
    provider = PermissionProvider(base)

    assert provider.get_feature(name).grant is None
    assert provider.get_currency(name).general is True


def test_the_provider_still_refuses_unknown_names_without_the_prefix():
    provider = PermissionProvider(DictProvider({}))

    with pytest.raises(DefinitionNotFoundError):
        provider.get_feature("send_email")
    with pytest.raises(DefinitionNotFoundError):
        provider.get_currency("credits")


def test_the_shortcuts_check_permissions_without_any_settings(alice):
    assert shortcuts.peek(permission_feature(PERM), 1, {"user": alice}).satisfied is False

    grant_permission(alice, PERM)

    assert shortcuts.peek(permission_feature(PERM), 1, {"user": alice}).satisfied is True


# -- granting and revoking ------------------------------------------------------------------------


def test_grant_permission_opens_a_grant_wallet_with_balance_one(alice):
    wallet = grant_permission(alice, PERM)

    row = Wallet.objects.get(pk=wallet.id)
    assert row.wallet_type == "grant"
    assert row.currency == permission_feature(PERM)
    assert row.balance == Decimal("1")
    assert row.source == PERMISSION_SOURCE
    assert row.valid_until is None


def test_a_plan_switch_leaves_a_granted_permission_alone(alice, backend):
    grant_permission(alice, PERM)

    shortcuts.plan_switch({"user": alice}, [])

    assert backend.has_perm(fresh(alice), PERM)


def test_revoke_permission_closes_every_live_grant_and_counts_them(alice, backend):
    grant_permission(alice, PERM)
    grant_permission(alice, PERM)
    grant_permission(alice, OTHER)

    assert revoke_permission(alice, PERM) == 2

    assert backend.get_all_permissions(fresh(alice)) == {OTHER}


def test_revoke_permission_leaves_a_grant_that_ends_at_that_instant(alice):
    end = timezone.now() + timedelta(days=1)
    grant_permission(alice, PERM, valid_until=end)

    assert revoke_permission(alice, PERM, at=end) == 0


def test_revoke_permission_without_a_grant_closes_nothing(alice):
    assert revoke_permission(alice, PERM) == 0


def test_revoke_permission_at_a_future_time_keeps_the_permission_until_then(alice, backend):
    grant_permission(alice, PERM)

    assert revoke_permission(alice, PERM, at=timezone.now() + timedelta(days=1)) == 1

    assert backend.has_perm(fresh(alice), PERM)


def test_revoke_permission_also_closes_a_grant_that_starts_later(alice, backend):
    grant_permission(alice, PERM, valid_from=timezone.now() + timedelta(days=1))

    assert revoke_permission(alice, PERM) == 1

    row = Wallet.objects.get()
    assert row.valid_until <= timezone.now()


# -- backend --------------------------------------------------------------------------------------


def test_a_granted_permission_is_held(alice, backend):
    grant_permission(alice, PERM)

    assert backend.has_perm(alice, PERM)
    assert not backend.has_perm(alice, OTHER)
    assert backend.get_all_permissions(alice) == {PERM}


def test_a_user_without_grants_holds_nothing(alice, backend):
    assert backend.get_all_permissions(alice) == set()
    assert not backend.has_perm(alice, PERM)
    assert not backend.has_module_perms(alice, "news")


def test_module_permissions_follow_the_app_label(alice, backend):
    grant_permission(alice, PERM)

    assert backend.has_module_perms(alice, "news")
    assert not backend.has_module_perms(alice, "new")
    assert not backend.has_module_perms(alice, "auth")


def test_wallets_in_other_currencies_are_not_permissions(alice, backend):
    shortcuts.open_wallet(
        alice,
        WalletSpec(WalletType.GRANT, "credits", Decimal("5")),
        source="purchase",
    )

    assert backend.get_all_permissions(alice) == set()


def test_a_grant_with_a_zero_balance_is_not_held(alice, backend):
    shortcuts.open_wallet(
        alice,
        WalletSpec(WalletType.GRANT, permission_feature(PERM), Decimal("0")),
        source=PERMISSION_SOURCE,
    )

    assert not backend.has_perm(alice, PERM)


def test_an_inactive_user_holds_nothing(alice, backend):
    grant_permission(alice, PERM)
    alice.is_active = False

    assert backend.get_all_permissions(alice) == set()
    assert not backend.has_perm(alice, PERM)
    assert not backend.has_module_perms(alice, "news")


def test_an_anonymous_user_holds_nothing(backend):
    assert backend.get_all_permissions(AnonymousUser()) == set()
    assert not backend.has_perm(AnonymousUser(), PERM)


def test_object_level_checks_are_not_answered(alice, backend):
    grant_permission(alice, PERM)

    assert backend.get_all_permissions(alice, obj=alice) == set()
    assert not backend.has_perm(alice, PERM, obj=alice)


def test_an_active_superuser_holds_every_django_permission(backend):
    root = User.objects.create(username="root", is_superuser=True)
    every = {f"{p.content_type.app_label}.{p.codename}" for p in Permission.objects.select_related("content_type")}

    assert every
    assert backend.get_all_permissions(root) == every
    assert backend.has_perm(root, "auth.add_user")


def test_the_backend_authenticates_nobody(alice, backend):
    assert backend.authenticate(None, username="alice", password="x") is None
    assert backend.get_user(alice.pk) is None


def test_permissions_are_cached_on_the_user_object(alice, backend):
    assert not backend.has_perm(alice, PERM)

    grant_permission(alice, PERM)

    assert not backend.has_perm(alice, PERM)
    assert backend.has_perm(fresh(alice), PERM)


def test_a_permission_of_a_group_is_held_by_its_members(alice, editors, backend):
    grant_permission(editors, PERM)
    shortcuts.add_member(alice, editors)

    assert backend.has_perm(alice, PERM)


def test_a_permission_of_an_enclosing_group_is_held_through_nesting(alice, editors, backend):
    staff = Group.objects.create(name="staff")
    grant_permission(staff, PERM)
    shortcuts.add_member(alice, editors)
    shortcuts.add_member(editors, staff)

    assert backend.get_all_permissions(alice) == {PERM}


def test_leaving_a_group_drops_its_permissions(alice, editors, backend):
    grant_permission(editors, PERM)
    shortcuts.add_member(alice, editors)

    shortcuts.remove_member(alice, editors)

    assert not backend.has_perm(fresh(alice), PERM)


def test_a_temporary_permission_is_held_only_inside_its_validity(alice, backend):
    now = timezone.now()
    grant_permission(alice, PERM, valid_until=now - timedelta(minutes=1))
    grant_permission(alice, OTHER, valid_from=now + timedelta(days=1))

    assert backend.get_all_permissions(alice) == set()

    grant_permission(alice, PERM, valid_until=now + timedelta(days=1))

    assert backend.get_all_permissions(fresh(alice)) == {PERM}


def test_a_revoked_permission_is_no_longer_held(alice, backend):
    grant_permission(alice, PERM)

    revoke_permission(alice, PERM)

    assert not backend.has_perm(fresh(alice), PERM)


# -- through Django -------------------------------------------------------------------------------


@override_settings(AUTHENTICATION_BACKENDS=[BACKEND])
def test_user_has_perm_asks_the_backend(alice):
    grant_permission(alice, PERM)

    user = fresh(alice)
    assert user.has_perm(PERM)
    assert user.has_module_perms("news")
    assert user.get_all_permissions() == {PERM}


@override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend", BACKEND])
def test_the_backend_chains_with_model_backend(alice):
    alice.user_permissions.add(Permission.objects.get(codename="add_user", content_type__app_label="auth"))
    grant_permission(alice, PERM)

    user = fresh(alice)
    assert user.has_perm("auth.add_user")
    assert user.has_perm(PERM)
    assert user.get_all_permissions() == {"auth.add_user", PERM}
