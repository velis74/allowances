from decimal import Decimal

import pytest

from django.contrib.auth.models import User

from django_licensing.engine import fulfill, InsufficientBalanceError
from django_licensing.models import Transaction, TransactionKind, Wallet, WalletType
from django_licensing.providers import SettingsProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def alice():
    return User.objects.create(username="alice")


@pytest.fixture
def bob():
    return User.objects.create(username="bob")


def provider_with(features, currencies):
    return SettingsProvider(config={"features": features, "currencies": currencies})


def test_fulfill_single_wallet_single_currency(alice):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"user"},
                "fulfillment_order": [("consumable", "user")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 3, {"user": alice}, provider=provider)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("7")
    assert result.requested == Decimal("3")
    assert result.granted == Decimal("3")
    assert result.satisfied is True
    assert result.modifier_applied is None
    assert len(result.transactions) == 1
    txn = result.transactions[0]
    assert txn.kind == TransactionKind.CONSUMPTION
    assert txn.amount == Decimal("-3")
    assert txn.wallet_id == wallet.id
    assert txn.fulfillment_record.guid == result.guid


def test_fulfill_walks_fulfillment_order_grant_before_consumable(alice, bob):
    # overdraft_limit=0 (not None) keeps this wallet out of the "drained -> archive" trigger so the
    # test can inspect it via refresh_from_db() after being drawn down to exactly zero.
    grant_wallet = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.GRANT,
        currency="credits",
        balance=Decimal("2"),
        overdraft_limit=Decimal("0"),
    )
    consumable_wallet = Wallet.objects.create(
        subject=bob, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("5")
    )
    provider = provider_with(
        features={
            "send_email": {
                "grant": {"currencies": ["credits"]},
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device", "team"},
                "fulfillment_order": [("grant", "device"), ("consumable", "team")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 4, {"device": alice, "team": bob}, provider=provider)

    grant_wallet.refresh_from_db()
    consumable_wallet.refresh_from_db()
    assert grant_wallet.balance == Decimal("0")
    assert consumable_wallet.balance == Decimal("3")
    assert result.granted == Decimal("4")
    assert result.satisfied is True
    assert len(result.transactions) == 2
    assert result.transactions[0].wallet_id == grant_wallet.id
    assert result.transactions[0].amount == Decimal("-2")
    assert result.transactions[1].wallet_id == consumable_wallet.id
    assert result.transactions[1].amount == Decimal("-2")


def test_fulfill_wildcard_subject_walks_subjects_in_order(alice, bob):
    # overdraft_limit=0 (not None) keeps this wallet out of the "drained -> archive" trigger.
    wallet_a = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.GRANT,
        currency="credits",
        balance=Decimal("2"),
        overdraft_limit=Decimal("0"),
    )
    wallet_b = Wallet.objects.create(
        subject=bob, wallet_type=WalletType.GRANT, currency="credits", balance=Decimal("5")
    )
    provider = provider_with(
        features={
            "send_email": {
                "grant": {"currencies": ["credits"]},
                "subjects": {"device", "team"},
                "fulfillment_order": [("grant", None)],
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 4, {"device": alice, "team": bob}, provider=provider)

    wallet_a.refresh_from_db()
    wallet_b.refresh_from_db()
    # alice's wallet (first in the subjects dict) is drained first, bob's covers the remainder.
    assert wallet_a.balance == Decimal("0")
    assert wallet_b.balance == Decimal("3")
    assert result.satisfied is True


def test_fulfill_shorthand_string_fulfillment_order_entry(alice):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("5")
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"location"},
                "fulfillment_order": ["location"],  # shorthand for (None, "location")
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 2, {"location": alice}, provider=provider)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("3")
    assert result.satisfied is True


def test_fulfill_currency_conversion_rate(alice):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    provider = provider_with(
        features={
            "coolant_fill": {
                "consumable": {"currencies": [("credits", "2")]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("coolant_fill", 3, {"device": alice}, provider=provider)

    wallet.refresh_from_db()
    # 3 units of coolant at a rate of 2 credits/unit costs 6 credits.
    assert wallet.balance == Decimal("4")
    assert result.granted == Decimal("3")
    assert result.transactions[0].amount == Decimal("-6")


def test_fulfill_dotted_callable_fulfillment_order(alice):
    small = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("2")
    )
    big = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("9")
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": "tests.fixtures_ordering.order_wallets",
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 5, {"device": alice}, provider=provider)

    small.refresh_from_db()
    big.refresh_from_db()
    # order_wallets sorts balance descending, so the bigger wallet is drained first.
    assert big.balance == Decimal("4")
    assert small.balance == Decimal("2")
    assert result.satisfied is True


def test_fulfill_overdraft_second_pass(alice):
    wallet = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("5"),
        overdraft_limit=Decimal("3"),
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
                "overdraft": True,
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 7, {"device": alice}, provider=provider)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("-2")
    assert wallet.first_overdraft_at is not None
    assert result.satisfied is True
    assert result.granted == Decimal("7")
    assert len(result.transactions) == 2
    assert result.transactions[0].amount == Decimal("-5")
    assert result.transactions[1].amount == Decimal("-2")


def test_fulfill_without_overdraft_flag_does_not_dip_into_negative(alice):
    wallet = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("5"),
        overdraft_limit=Decimal("3"),
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
                "overdraft": False,
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 7, {"device": alice}, allow_partial=True, provider=provider)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("0")
    assert result.satisfied is False
    assert result.granted == Decimal("5")


def test_fulfill_raises_and_rolls_back_when_not_allow_partial(alice):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("2")
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    with pytest.raises(InsufficientBalanceError) as exc_info:
        fulfill("send_email", 5, {"device": alice}, provider=provider)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("2")
    assert Transaction.objects.count() == 0
    assert exc_info.value.result.granted == Decimal("2")
    assert exc_info.value.result.satisfied is False


def test_fulfill_allow_partial_persists_partial_draw(alice):
    # overdraft_limit=0 (not None) keeps this wallet out of the "drained -> archive" trigger.
    wallet = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("2"),
        overdraft_limit=Decimal("0"),
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 5, {"device": alice}, allow_partial=True, provider=provider)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("0")
    assert result.satisfied is False
    assert result.granted == Decimal("2")
    assert Transaction.objects.count() == 1


def test_fulfill_modifier_uses_max_of_applicable_modifiers(alice):
    from django_licensing.models import LicenseModifier

    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    LicenseModifier.objects.create(subject=alice, feature="send_email", factor=Decimal("0.5"))
    LicenseModifier.objects.create(subject=alice, feature="send_email", factor=Decimal("0.8"))
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 5, {"device": alice}, provider=provider)

    wallet.refresh_from_db()
    # README: "the most favourable (max) is used" — taken literally, max(0.5, 0.8) = 0.8.
    assert result.modifier_applied == Decimal("0.8")
    assert wallet.balance == Decimal("6")  # 10 - (5 * 0.8)


def test_fulfill_modifier_outside_validity_window_is_ignored(alice):
    from django.utils import timezone

    from django_licensing.models import LicenseModifier

    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    now = timezone.now()
    LicenseModifier.objects.create(
        subject=alice,
        feature="send_email",
        factor=Decimal("0.1"),
        valid_until=now - timezone.timedelta(days=1),
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 5, {"device": alice}, provider=provider, at=now)

    wallet.refresh_from_db()
    assert result.modifier_applied is None
    assert wallet.balance == Decimal("5")


def test_fulfill_rounding_floor(alice):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": "floor"}},
    )

    result = fulfill("send_email", "2.7", {"device": alice}, allow_partial=True, provider=provider)

    wallet.refresh_from_db()
    assert wallet.balance == Decimal("8")  # floor(2.7) = 2 charged
    assert result.granted == Decimal("2")
    assert result.satisfied is False


def test_fulfill_rounding_ceil(alice):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="tokens", balance=Decimal("5")
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": [("tokens", "0.3")]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"tokens": {"general": False, "round_fn": "ceil"}},
    )

    result = fulfill("send_email", 1, {"device": alice}, provider=provider)

    wallet.refresh_from_db()
    # raw cost = 1 * 0.3 = 0.3, ceil -> 1 token charged.
    assert wallet.balance == Decimal("4")
    assert result.satisfied is True
    assert result.granted == Decimal("1")


def test_fulfill_rounding_round_half_up(alice):
    wallet = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": "round"}},
    )

    result = fulfill("send_email", "2.5", {"device": alice}, provider=provider)

    wallet.refresh_from_db()
    # raw cost = 2.5, round-half-up -> 3 credits charged, but only 2.5 units were requested.
    assert wallet.balance == Decimal("7")
    assert result.satisfied is True
    assert result.granted == Decimal("2.5")


def test_fulfill_respects_wallet_validity_window(alice):
    from django.utils import timezone

    now = timezone.now()
    expired = Wallet.objects.create(
        subject=alice,
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("10"),
        valid_until=now - timezone.timedelta(days=1),
    )
    active = Wallet.objects.create(
        subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("4")
    )
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )

    result = fulfill("send_email", 3, {"device": alice}, provider=provider, at=now)

    expired.refresh_from_db()
    active.refresh_from_db()
    assert expired.balance == Decimal("10")  # untouched — outside its validity window
    assert active.balance == Decimal("1")
    assert result.satisfied is True


def test_fulfill_guid_can_be_supplied_to_group_multiple_calls(alice):
    import uuid

    Wallet.objects.create(subject=alice, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10"))
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": None}},
    )
    guid = uuid.uuid4()

    result1 = fulfill("send_email", 2, {"device": alice}, guid=guid, provider=provider)
    result2 = fulfill("send_email", 3, {"device": alice}, guid=guid, provider=provider)

    assert result1.guid == guid
    assert result2.guid == guid
    from django_licensing.models import FulfillmentRecord

    record = FulfillmentRecord.objects.get(guid=guid)
    assert record.transactions.count() == 2
