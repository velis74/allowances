"""
Scenarios for Django permissions held as allowances grants, with ``auth.User`` and ``auth.Group``
as subjects and ``AllowancesBackend`` answering ``has_perm``.
"""

from datetime import timedelta

import pytest

from django.contrib.auth.decorators import permission_required
from django.contrib.auth.models import AnonymousUser, Group, Permission, User
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import override_settings, RequestFactory
from django.utils import timezone

from allowances.errors import InsufficientBalanceError
from allowances.types import TransactionKind
from allowances_django import shortcuts
from allowances_django.models import Transaction
from allowances_django.permissions import (
    grant_permission,
    permission_feature,
    PERMISSION_PREFIX,
    revoke_permission,
)
from allowances_django.subjects import subject_ref

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("allowances_backend"),
]

PERM = "news.change_article"
BACKEND = "allowances_django.permissions.AllowancesBackend"


@pytest.fixture
def allowances_backend():
    with override_settings(AUTHENTICATION_BACKENDS=[BACKEND]):
        yield


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


@pytest.fixture
def editors():
    return Group.objects.create(name="editors")


def fresh(user):
    """The same user as a new object, without the permission cache of the old one."""
    return User.objects.get(pk=user.pk)


# -- naming ---------------------------------------------------------------------------------------


def test_a_permissions_feature_name_carries_the_prefix():
    assert PERMISSION_PREFIX == "perm:"
    assert permission_feature(PERM) == "perm:news.change_article"


# -- direct grants --------------------------------------------------------------------------------


def test_a_user_without_a_grant_lacks_the_permission(alice):
    assert alice.has_perm(PERM) is False


def test_a_granted_permission_is_held(alice):
    grant_permission(alice, PERM)

    assert fresh(alice).has_perm(PERM) is True


def test_a_grant_does_not_leak_to_another_user(alice):
    bob = User.objects.create(username="bob")
    grant_permission(alice, PERM)

    assert fresh(bob).has_perm(PERM) is False


def test_a_grant_is_one_permission_only(alice):
    grant_permission(alice, PERM)

    assert fresh(alice).has_perm("news.delete_article") is False


def test_a_granted_permission_is_listed_in_all_permissions(alice):
    grant_permission(alice, PERM)
    grant_permission(alice, "news.add_article")

    assert fresh(alice).get_all_permissions() == {PERM, "news.add_article"}


def test_grant_permission_opens_a_grant_wallet_of_one(alice):
    wallet = grant_permission(alice, PERM)

    assert wallet.currency == "perm:news.change_article"
    assert wallet.wallet_type == "grant"
    assert wallet.balance == 1
    assert wallet.source == "permission"


def test_revoking_removes_the_permission(alice):
    grant_permission(alice, PERM)

    assert revoke_permission(alice, PERM) == 1
    assert fresh(alice).has_perm(PERM) is False


def test_revoking_counts_every_live_grant_of_that_permission(alice):
    grant_permission(alice, PERM)
    grant_permission(alice, PERM)
    grant_permission(alice, "news.add_article")

    assert revoke_permission(alice, PERM) == 2
    assert fresh(alice).has_perm("news.add_article") is True


def test_revoking_a_permission_never_granted_revokes_nothing(alice):
    assert revoke_permission(alice, PERM) == 0


def test_a_plan_switch_leaves_permissions_alone(alice):
    grant_permission(alice, PERM)

    shortcuts.plan_switch({"user": alice}, [])

    assert fresh(alice).has_perm(PERM) is True


# -- temporary grants -----------------------------------------------------------------------------


def test_a_temporary_permission_is_held_until_it_ends(alice):
    grant_permission(alice, PERM, valid_until=timezone.now() + timedelta(hours=1))

    assert fresh(alice).has_perm(PERM) is True


def test_a_permission_starting_later_is_not_held_yet(alice):
    grant_permission(alice, PERM, valid_from=timezone.now() + timedelta(hours=1))

    assert fresh(alice).has_perm(PERM) is False


def test_an_ended_permission_is_not_held(alice):
    now = timezone.now()
    grant_permission(alice, PERM, valid_from=now - timedelta(days=2), valid_until=now - timedelta(days=1))

    assert fresh(alice).has_perm(PERM) is False


# -- groups ---------------------------------------------------------------------------------------


def test_a_member_holds_its_groups_permission(alice, editors):
    grant_permission(editors, PERM)
    shortcuts.add_member(alice, editors)

    assert fresh(alice).has_perm(PERM) is True


def test_a_member_holds_a_permission_of_a_nested_group(alice, editors):
    staff = Group.objects.create(name="staff")
    grant_permission(staff, PERM)
    shortcuts.add_member(alice, editors)
    shortcuts.add_member(editors, staff)

    assert fresh(alice).has_perm(PERM) is True


def test_leaving_the_group_drops_its_permission(alice, editors):
    grant_permission(editors, PERM)
    shortcuts.add_member(alice, editors)

    assert shortcuts.remove_member(alice, editors) is True
    assert fresh(alice).has_perm(PERM) is False


def test_revoking_from_the_group_removes_it_from_members(alice, editors):
    grant_permission(editors, PERM)
    shortcuts.add_member(alice, editors)

    revoke_permission(editors, PERM)

    assert fresh(alice).has_perm(PERM) is False


def test_groups_of_returns_model_instances(alice, editors):
    staff = Group.objects.create(name="staff")
    shortcuts.add_member(alice, editors)
    shortcuts.add_member(editors, staff)

    assert shortcuts.groups_of(alice) == [editors, staff]


# -- user state and caching -----------------------------------------------------------------------


def test_an_inactive_user_holds_no_permission(alice):
    grant_permission(alice, PERM)
    User.objects.filter(pk=alice.pk).update(is_active=False)

    user = fresh(alice)

    assert user.has_perm(PERM) is False
    assert user.has_module_perms("news") is False


def test_an_anonymous_user_holds_no_permission():
    assert AnonymousUser().has_perm(PERM) is False


def test_an_active_superuser_holds_every_permission():
    root = User.objects.create(username="root", is_superuser=True)

    assert root.has_perm("auth.add_user") is True
    assert "auth.change_group" in root.get_all_permissions()
    assert len(root.get_all_permissions()) == Permission.objects.count()


def test_object_level_checks_are_not_answered(alice):
    grant_permission(alice, PERM)

    assert fresh(alice).has_perm(PERM, obj=alice) is False


def test_module_permissions_follow_the_granted_permissions(alice):
    grant_permission(alice, PERM)

    user = fresh(alice)

    assert user.has_module_perms("news") is True
    assert user.has_module_perms("shop") is False


def test_permissions_are_cached_for_the_lifetime_of_the_user_object(alice):
    assert alice.has_perm(PERM) is False

    grant_permission(alice, PERM)

    assert alice.has_perm(PERM) is False
    assert fresh(alice).has_perm(PERM) is True


# -- with Django's own backend --------------------------------------------------------------------


@override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend", BACKEND])
def test_chained_with_model_backend_both_sources_count(alice):
    alice.user_permissions.add(Permission.objects.get(codename="add_user", content_type__app_label="auth"))
    grant_permission(alice, PERM)

    user = fresh(alice)

    assert user.has_perm("auth.add_user") is True
    assert user.has_perm(PERM) is True


def test_a_view_guarded_by_permission_required_admits_a_granted_user(alice):
    @permission_required(PERM, raise_exception=True)
    def edit(request):
        return HttpResponse("ok")

    request = RequestFactory().get("/edit")
    request.user = fresh(alice)
    with pytest.raises(PermissionDenied):
        edit(request)

    grant_permission(alice, PERM)
    request.user = fresh(alice)

    assert edit(request).status_code == 200


# -- audited use ----------------------------------------------------------------------------------


def test_peek_checks_a_permission_without_a_ledger_entry(alice):
    grant_permission(alice, PERM)

    result = shortcuts.peek(permission_feature(PERM), 1, {"user": alice})

    assert result.satisfied is True
    assert Transaction.objects.count() == 0


def test_using_a_permission_leaves_a_ledger_entry_with_the_user_as_actor(alice):
    grant_permission(alice, PERM)

    result = shortcuts.fulfill(permission_feature(PERM), 1, {"user": alice})

    assert result.satisfied is True
    [entry] = result.transactions
    assert entry.kind == TransactionKind.CONSUMPTION
    assert entry.amount == 0
    assert entry.actor == subject_ref(alice)
    row = Transaction.objects.get()
    assert (row.actor_type, row.actor_id) == ("auth.user", str(alice.pk))
    assert row.amount == 0


def test_a_use_of_a_group_permission_records_the_member_as_actor(alice, editors):
    grant_permission(editors, PERM)
    shortcuts.add_member(alice, editors)

    result = shortcuts.fulfill(permission_feature(PERM), 1, {"user": alice})

    [entry] = result.transactions
    assert entry.actor == subject_ref(alice)
    assert Transaction.objects.get().wallet.subject_id == str(editors.pk)


def test_using_a_permission_not_held_is_refused(alice):
    with pytest.raises(InsufficientBalanceError):
        shortcuts.fulfill(permission_feature(PERM), 1, {"user": alice})

    assert Transaction.objects.count() == 0


def test_an_audited_use_can_be_attached_to_the_object_it_was_used_on(alice, editors):
    grant_permission(alice, PERM)

    result = shortcuts.fulfill(permission_feature(PERM), 1, {"user": alice})
    shortcuts.attach(result.guid, editors)

    row = Transaction.objects.get()
    assert (row.object_type, row.object_id) == ("auth.group", str(editors.pk))
    assert shortcuts.find_unattached_fulfillment_records() == []
