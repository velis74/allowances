"""
``shortcuts.effective_allowances`` with model instances, the ``effective_permissions`` converter, and
``AllowancesBackend`` reading its permission set through both.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from django.contrib.auth.models import Group, User
from django.utils import timezone

from allowances.types import Allowance, SubjectRef, Wallet, WalletSpec, WalletType
from allowances_django import shortcuts
from allowances_django.permissions import (
    AllowancesBackend,
    effective_permissions,
    grant_permission,
    PERMISSION_SOURCE,
    revoke_permission,
)
from allowances_django.subjects import subject_ref

PERM = "news.change_article"
OTHER = "news.delete_article"
DEVICE = SubjectRef("device", "kiosk-7")


def _allowance(currency, balance="1", *, wallet_type=WalletType.GRANT, holder=DEVICE, depth=0, wallet_id=1):
    wallet = Wallet(
        id=wallet_id,
        subject=holder,
        wallet_type=wallet_type,
        currency=currency,
        balance=Decimal(balance),
        created_at=timezone.now(),
    )
    return Allowance(wallet=wallet, holder=holder, depth=depth, balance=Decimal(balance))


# -- effective_permissions --------------------------------------------------------------------------


def test_the_converter_keeps_positive_permission_grants_keyed_by_permission():
    held = _allowance("perm:" + PERM)

    assert effective_permissions([held]) == {PERM: [held]}


def test_the_converter_drops_consumables_other_currencies_and_non_positive_balances():
    allowances = [
        _allowance("perm:" + PERM, wallet_type=WalletType.CONSUMABLE, wallet_id=1),
        _allowance("credits", "10", wallet_id=2),
        _allowance("perm:" + OTHER, "0", wallet_id=3),
        _allowance("perm:" + OTHER, "-1", wallet_id=4),
    ]

    assert effective_permissions(allowances) == {}


def test_the_converter_lists_every_holder_of_a_permission_in_input_order_with_sorted_keys():
    team = SubjectRef("team", 1)
    own = _allowance("perm:" + PERM, wallet_id=1)
    other = _allowance("perm:" + OTHER, holder=team, depth=1, wallet_id=2)
    inherited = _allowance("perm:" + PERM, holder=team, depth=1, wallet_id=3)

    result = effective_permissions(iter([other, own, inherited]))

    assert list(result) == [PERM, OTHER]
    assert result[PERM] == [own, inherited]
    assert result[OTHER] == [other]


def test_the_converter_of_nothing_is_empty():
    assert effective_permissions([]) == {}


# -- shortcuts.effective_allowances -----------------------------------------------------------------


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


@pytest.fixture
def editors():
    return Group.objects.create(name="editors")


@pytest.mark.django_db
def test_the_shortcut_takes_a_user_and_lists_wallets_of_its_groups(alice, editors):
    staff = Group.objects.create(name="staff")
    shortcuts.add_member(alice, editors)
    shortcuts.add_member(editors, staff)
    own = grant_permission(alice, OTHER)
    inherited = grant_permission(editors, PERM)
    nested = grant_permission(staff, PERM)
    credits = shortcuts.open_wallet(
        alice, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("5")), source="purchase"
    )

    allowances = shortcuts.effective_allowances(alice)

    assert [(a.wallet.id, a.holder, a.depth) for a in allowances] == [
        (own.id, subject_ref(alice), 0),
        (credits.id, subject_ref(alice), 0),
        (inherited.id, subject_ref(editors), 1),
        (nested.id, subject_ref(staff), 2),
    ]
    assert [a.balance for a in allowances] == [Decimal("1"), Decimal("5"), Decimal("1"), Decimal("1")]
    assert [a.wallet.source for a in allowances] == [
        PERMISSION_SOURCE,
        "purchase",
        PERMISSION_SOURCE,
        PERMISSION_SOURCE,
    ]


@pytest.mark.django_db
def test_the_shortcut_passes_its_filters_through(alice):
    grant_permission(alice, PERM)
    shortcuts.open_wallet(alice, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("5")), source="purchase")

    grants = shortcuts.effective_allowances(alice, wallet_types=[WalletType.GRANT])
    credits = shortcuts.effective_allowances(alice, currencies=["credits"])

    assert [a.wallet.currency for a in grants] == ["perm:" + PERM]
    assert [a.wallet.currency for a in credits] == ["credits"]


@pytest.mark.django_db
def test_the_shortcut_answers_for_a_subject_that_is_not_a_model(editors):
    shortcuts.add_member(DEVICE, editors)
    grant_permission(editors, PERM)
    grant_permission(DEVICE, OTHER)

    allowances = shortcuts.effective_allowances(DEVICE)

    assert [(a.holder, a.depth) for a in allowances] == [(DEVICE, 0), (subject_ref(editors), 1)]
    assert set(effective_permissions(allowances)) == {PERM, OTHER}


@pytest.mark.django_db
def test_the_shortcut_honours_at(alice):
    grant_permission(alice, PERM, valid_until=timezone.now() + timedelta(days=1))
    now = timezone.now()

    assert len(shortcuts.effective_allowances(alice, at=now)) == 1
    assert shortcuts.effective_allowances(alice, at=now + timedelta(days=2)) == []


# -- AllowancesBackend ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_backend_permission_set_is_the_converter_keys(alice, editors):
    shortcuts.add_member(alice, editors)
    grant_permission(alice, OTHER)
    grant_permission(editors, PERM)
    shortcuts.open_wallet(alice, WalletSpec(WalletType.GRANT, "perm:news.view_article", Decimal("0")), source="x")

    expected = set(effective_permissions(shortcuts.effective_allowances(alice)))

    assert expected == {PERM, OTHER}
    assert AllowancesBackend().get_all_permissions(alice) == expected


@pytest.mark.django_db
def test_revoked_and_expired_permissions_disappear_from_both(alice, editors):
    now = timezone.now()
    shortcuts.add_member(alice, editors)
    grant_permission(editors, PERM)
    grant_permission(alice, OTHER, valid_from=now - timedelta(days=1), valid_until=now - timedelta(minutes=1))

    revoke_permission(editors, PERM)

    assert effective_permissions(shortcuts.effective_allowances(alice)) == {}
    assert AllowancesBackend().get_all_permissions(User.objects.get(pk=alice.pk)) == set()
