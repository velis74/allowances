"""
``allowances_django.permissions``: the permission provider, granting and revoking, and
``AllowancesBackend`` called directly and through Django's ``User.has_perm``. Tests that grant, revoke or check run
once per flavour (``dj``): the synchronous API and checks (``has_perm``), and the asynchronous ones (``ahas_perm``).
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
from allowances_django.models import Wallet
from allowances_django.permissions import (
    AllowancesBackend,
    permission_feature,
    PERMISSION_PREFIX,
    PERMISSION_SOURCE,
    PermissionProvider,
)

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


@pytest.mark.django_db
def test_permission_feature_prefixes_the_permission():
    assert PERMISSION_PREFIX == "perm:"
    assert permission_feature(PERM) == "perm:news.change_article"


@pytest.mark.django_db
def test_the_provider_serves_a_rate_zero_grant_feature_for_a_permission():
    feature = PermissionProvider(DictProvider({})).get_feature(permission_feature(PERM))

    assert feature.consumable is None
    assert feature.grant.rate_for(permission_feature(PERM)) == Decimal("0")
    assert list(feature.fulfillment_order) == [(WalletType.GRANT, None, None)]
    assert feature.subjects == frozenset()


@pytest.mark.django_db
def test_the_provider_serves_a_plain_currency_for_a_permission():
    currency = PermissionProvider(DictProvider({})).get_currency(permission_feature(PERM))

    assert currency.name == permission_feature(PERM)
    assert currency.general is False
    assert currency.round_fn is None


@pytest.mark.django_db
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


@pytest.mark.django_db
def test_the_provider_still_refuses_unknown_names_without_the_prefix():
    provider = PermissionProvider(DictProvider({}))

    with pytest.raises(DefinitionNotFoundError):
        provider.get_feature("send_email")
    with pytest.raises(DefinitionNotFoundError):
        provider.get_currency("credits")


def test_the_shortcuts_check_permissions_without_any_settings(dj, alice):
    assert dj.peek(permission_feature(PERM), 1, {"user": alice}).satisfied is False

    dj.grant_permission(alice, PERM)

    assert dj.peek(permission_feature(PERM), 1, {"user": alice}).satisfied is True


# -- granting and revoking ------------------------------------------------------------------------


def test_grant_permission_opens_a_grant_wallet_with_balance_one(dj, alice):
    wallet = dj.grant_permission(alice, PERM)

    row = Wallet.objects.get(pk=wallet.id)
    assert row.wallet_type == "grant"
    assert row.currency == permission_feature(PERM)
    assert row.balance == Decimal("1")
    assert row.source == PERMISSION_SOURCE
    assert row.valid_until is None


def test_a_plan_switch_leaves_a_granted_permission_alone(dj, alice, backend):
    dj.grant_permission(alice, PERM)

    dj.plan_switch({"user": alice}, [])

    assert dj.has_perm(backend, fresh(alice), PERM)


def test_revoke_permission_closes_every_live_grant_and_counts_them(dj, alice, backend):
    dj.grant_permission(alice, PERM)
    dj.grant_permission(alice, PERM)
    dj.grant_permission(alice, OTHER)

    assert dj.revoke_permission(alice, PERM) == 2

    assert dj.get_all_permissions(backend, fresh(alice)) == {OTHER}


def test_revoke_permission_leaves_a_grant_that_ends_at_that_instant(dj, alice):
    end = timezone.now() + timedelta(days=1)
    dj.grant_permission(alice, PERM, valid_until=end)

    assert dj.revoke_permission(alice, PERM, at=end) == 0


def test_revoke_permission_without_a_grant_closes_nothing(dj, alice):
    assert dj.revoke_permission(alice, PERM) == 0


def test_revoke_permission_at_a_future_time_keeps_the_permission_until_then(dj, alice, backend):
    dj.grant_permission(alice, PERM)

    assert dj.revoke_permission(alice, PERM, at=timezone.now() + timedelta(days=1)) == 1

    assert dj.has_perm(backend, fresh(alice), PERM)


def test_revoke_permission_also_closes_a_grant_that_starts_later(dj, alice, backend):
    dj.grant_permission(alice, PERM, valid_from=timezone.now() + timedelta(days=1))

    assert dj.revoke_permission(alice, PERM) == 1

    row = Wallet.objects.get()
    assert row.valid_until <= timezone.now()


# -- backend --------------------------------------------------------------------------------------


def test_a_granted_permission_is_held(dj, alice, backend):
    dj.grant_permission(alice, PERM)

    assert dj.has_perm(backend, alice, PERM)
    assert not dj.has_perm(backend, alice, OTHER)
    assert dj.get_all_permissions(backend, alice) == {PERM}


def test_a_user_without_grants_holds_nothing(dj, alice, backend):
    assert dj.get_all_permissions(backend, alice) == set()
    assert not dj.has_perm(backend, alice, PERM)
    assert not dj.has_module_perms(backend, alice, "news")


def test_module_permissions_follow_the_app_label(dj, alice, backend):
    dj.grant_permission(alice, PERM)

    assert dj.has_module_perms(backend, alice, "news")
    assert not dj.has_module_perms(backend, alice, "new")
    assert not dj.has_module_perms(backend, alice, "auth")


def test_wallets_in_other_currencies_are_not_permissions(dj, alice, backend):
    dj.open_wallet(
        alice,
        WalletSpec(WalletType.GRANT, "credits", Decimal("5")),
        source="purchase",
    )

    assert dj.get_all_permissions(backend, alice) == set()


def test_a_grant_with_a_zero_balance_is_not_held(dj, alice, backend):
    dj.open_wallet(
        alice,
        WalletSpec(WalletType.GRANT, permission_feature(PERM), Decimal("0")),
        source=PERMISSION_SOURCE,
    )

    assert not dj.has_perm(backend, alice, PERM)


def test_an_inactive_user_holds_nothing(dj, alice, backend):
    dj.grant_permission(alice, PERM)
    alice.is_active = False

    assert dj.get_all_permissions(backend, alice) == set()
    assert not dj.has_perm(backend, alice, PERM)
    assert not dj.has_module_perms(backend, alice, "news")


def test_an_anonymous_user_holds_nothing(dj, backend):
    assert dj.get_all_permissions(backend, AnonymousUser()) == set()
    assert not dj.has_perm(backend, AnonymousUser(), PERM)


def test_object_level_checks_are_not_answered(dj, alice, backend):
    dj.grant_permission(alice, PERM)

    assert dj.get_all_permissions(backend, alice, obj=alice) == set()
    assert not dj.has_perm(backend, alice, PERM, obj=alice)


def test_an_active_superuser_holds_every_django_permission(dj, backend):
    root = User.objects.create(username="root", is_superuser=True)
    every = {f"{p.content_type.app_label}.{p.codename}" for p in Permission.objects.select_related("content_type")}

    assert every
    assert dj.get_all_permissions(backend, root) == every
    assert dj.has_perm(backend, root, "auth.add_user")


@pytest.mark.django_db
def test_the_backend_authenticates_nobody(alice, backend):
    assert backend.authenticate(None, username="alice", password="x") is None
    assert backend.get_user(alice.pk) is None


def test_permissions_are_cached_on_the_user_object(dj, alice, backend):
    assert not dj.has_perm(backend, alice, PERM)

    dj.grant_permission(alice, PERM)

    assert not dj.has_perm(backend, alice, PERM)
    assert dj.has_perm(backend, fresh(alice), PERM)


def test_a_permission_of_a_group_is_held_by_its_members(dj, alice, editors, backend):
    dj.grant_permission(editors, PERM)
    dj.add_member(alice, editors)

    assert dj.has_perm(backend, alice, PERM)


def test_a_permission_of_an_enclosing_group_is_held_through_nesting(dj, alice, editors, backend):
    staff = Group.objects.create(name="staff")
    dj.grant_permission(staff, PERM)
    dj.add_member(alice, editors)
    dj.add_member(editors, staff)

    assert dj.get_all_permissions(backend, alice) == {PERM}


def test_leaving_a_group_drops_its_permissions(dj, alice, editors, backend):
    dj.grant_permission(editors, PERM)
    dj.add_member(alice, editors)

    dj.remove_member(alice, editors)

    assert not dj.has_perm(backend, fresh(alice), PERM)


def test_a_temporary_permission_is_held_only_inside_its_validity(dj, alice, backend):
    now = timezone.now()
    dj.grant_permission(alice, PERM, valid_until=now - timedelta(minutes=1))
    dj.grant_permission(alice, OTHER, valid_from=now + timedelta(days=1))

    assert dj.get_all_permissions(backend, alice) == set()

    dj.grant_permission(alice, PERM, valid_until=now + timedelta(days=1))

    assert dj.get_all_permissions(backend, fresh(alice)) == {PERM}


def test_a_revoked_permission_is_no_longer_held(dj, alice, backend):
    dj.grant_permission(alice, PERM)

    dj.revoke_permission(alice, PERM)

    assert not dj.has_perm(backend, fresh(alice), PERM)


# -- through Django -------------------------------------------------------------------------------


@override_settings(AUTHENTICATION_BACKENDS=[BACKEND])
def test_user_has_perm_asks_the_backend(dj, alice):
    dj.grant_permission(alice, PERM)

    user = fresh(alice)
    assert dj.user_has_perm(user, PERM)
    assert dj.user_has_module_perms(user, "news")
    assert dj.user_get_all_permissions(user) == {PERM}


@override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend", BACKEND])
def test_the_backend_chains_with_model_backend(dj, alice):
    alice.user_permissions.add(Permission.objects.get(codename="add_user", content_type__app_label="auth"))
    dj.grant_permission(alice, PERM)

    user = fresh(alice)
    assert dj.user_has_perm(user, "auth.add_user")
    assert dj.user_has_perm(user, PERM)
    assert dj.user_get_all_permissions(user) == {"auth.add_user", PERM}
