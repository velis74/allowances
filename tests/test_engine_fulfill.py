"""``fulfill`` — the ordering walk, conversion rates, rounding, overdraft, modifiers, atomicity."""

import uuid

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances.engine import fulfill
from allowances.errors import InsufficientBalanceError
from allowances.types import TransactionKind, WalletType
from allowances.windows import utcnow
from tests.conftest import CREDITS, open_wallet, provider_with
from tests.helpers import add_modifier, transaction_count


def test_draws_from_a_single_wallet(store, alice, provider):
    wallet = open_wallet(store, alice, balance="10")

    result = fulfill(store, "send_email", 3, {"device": alice}, provider)

    assert store.get_wallet(wallet.id).balance == Decimal("7")
    assert result.requested == Decimal("3")
    assert result.granted == Decimal("3")
    assert result.satisfied is True
    assert result.modifier_applied is None
    assert len(result.transactions) == 1
    txn = result.transactions[0]
    assert txn.kind == TransactionKind.CONSUMPTION
    assert txn.amount == Decimal("-3")
    assert txn.wallet_id == wallet.id
    assert txn.record_guid == result.guid


def test_walks_fulfillment_order_steps_in_declared_sequence(store, alice, bob):
    # An explicit overdraft_limit of 0 keeps this wallet out of the drained-to-zero archive trigger,
    # so the test can still read its balance back afterwards.
    grant = open_wallet(store, alice, wallet_type=WalletType.GRANT, balance="2", overdraft_limit=Decimal("0"))
    consumable = open_wallet(store, bob, balance="5")
    provider = provider_with(
        features={
            "send_email": {
                "grant": {"currencies": ["credits"]},
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device", "team"},
                "fulfillment_order": [("grant", "device"), ("consumable", "team")],
            }
        },
        currencies=CREDITS,
    )

    result = fulfill(store, "send_email", 4, {"device": alice, "team": bob}, provider)

    assert store.get_wallet(grant.id).balance == Decimal("0")
    assert store.get_wallet(consumable.id).balance == Decimal("3")
    assert result.satisfied is True
    assert [(t.wallet_id, t.amount) for t in result.transactions] == [
        (grant.id, Decimal("-2")),
        (consumable.id, Decimal("-2")),
    ]


def test_a_wildcard_subject_step_walks_every_subject_in_order(store, alice, bob):
    first = open_wallet(store, alice, wallet_type=WalletType.GRANT, balance="2", overdraft_limit=Decimal("0"))
    second = open_wallet(store, bob, wallet_type=WalletType.GRANT, balance="5")
    provider = provider_with(
        features={
            "send_email": {
                "grant": {"currencies": ["credits"]},
                "subjects": {"device", "team"},
                "fulfillment_order": [("grant", None)],
            }
        },
        currencies=CREDITS,
    )

    result = fulfill(store, "send_email", 4, {"device": alice, "team": bob}, provider)

    # The subjects dict is walked in its own order, so alice's wallet drains first.
    assert store.get_wallet(first.id).balance == Decimal("0")
    assert store.get_wallet(second.id).balance == Decimal("3")
    assert result.satisfied is True


def test_a_step_naming_an_unsupplied_subject_is_skipped_not_an_error(store, alice):
    wallet = open_wallet(store, alice, balance="5")
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device", "team"},
                "fulfillment_order": [("consumable", "team"), ("consumable", "device")],
            }
        },
        currencies=CREDITS,
    )

    result = fulfill(store, "send_email", 2, {"device": alice}, provider)

    assert result.satisfied is True
    assert store.get_wallet(wallet.id).balance == Decimal("3")


def test_shorthand_string_step_matches_any_wallet_type(store, alice):
    wallet = open_wallet(store, alice, balance="5")
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"location"},
                "fulfillment_order": ["location"],
            }
        },
        currencies=CREDITS,
    )

    result = fulfill(store, "send_email", 2, {"location": alice}, provider)

    assert store.get_wallet(wallet.id).balance == Decimal("3")
    assert result.satisfied is True


def test_conversion_rate_multiplies_the_cost(store, alice):
    wallet = open_wallet(store, alice, balance="10")
    provider = provider_with(
        features={
            "coolant_fill": {
                "consumable": {"currencies": [("credits", "2")]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies=CREDITS,
    )

    result = fulfill(store, "coolant_fill", 3, {"device": alice}, provider)

    # Three units at two credits each.
    assert store.get_wallet(wallet.id).balance == Decimal("4")
    assert result.granted == Decimal("3")
    assert result.transactions[0].amount == Decimal("-6")


def test_a_callable_fulfillment_order_decides_the_sequence(store, alice):
    small = open_wallet(store, alice, balance="2")
    big = open_wallet(store, alice, balance="9")
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": "tests.fixtures_ordering.order_wallets",
            }
        },
        currencies=CREDITS,
    )

    result = fulfill(store, "send_email", 5, {"device": alice}, provider)

    # order_wallets sorts by balance descending, so the bigger wallet goes first.
    assert store.get_wallet(big.id).balance == Decimal("4")
    assert store.get_wallet(small.id).balance == Decimal("2")
    assert result.satisfied is True


def test_overdraft_makes_a_second_pass_over_wallets_with_headroom(store, alice):
    wallet = open_wallet(store, alice, balance="5", overdraft_limit=Decimal("3"))
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
                "overdraft": True,
            }
        },
        currencies=CREDITS,
    )

    result = fulfill(store, "send_email", 7, {"device": alice}, provider)

    reloaded = store.get_wallet(wallet.id)
    assert reloaded.balance == Decimal("-2")
    assert reloaded.first_overdraft_at is not None
    assert result.satisfied is True
    assert [t.amount for t in result.transactions] == [Decimal("-5"), Decimal("-2")]


def test_without_the_overdraft_flag_a_limit_is_never_used(store, alice):
    wallet = open_wallet(store, alice, balance="5", overdraft_limit=Decimal("3"))
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
                "overdraft": False,
            }
        },
        currencies=CREDITS,
    )

    result = fulfill(store, "send_email", 7, {"device": alice}, provider, allow_partial=True)

    assert store.get_wallet(wallet.id).balance == Decimal("0")
    assert result.satisfied is False
    assert result.granted == Decimal("5")


def test_a_request_that_cannot_be_met_raises_and_rolls_everything_back(store, alice, provider):
    wallet = open_wallet(store, alice, balance="2")

    with pytest.raises(InsufficientBalanceError) as exc_info:
        fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert store.get_wallet(wallet.id).balance == Decimal("2")
    assert transaction_count(store) == 0
    assert exc_info.value.result.granted == Decimal("2")
    assert exc_info.value.result.satisfied is False


def test_allow_partial_keeps_whatever_was_available(store, alice, provider):
    wallet = open_wallet(store, alice, balance="2", overdraft_limit=Decimal("0"))

    result = fulfill(store, "send_email", 5, {"device": alice}, provider, allow_partial=True)

    assert store.get_wallet(wallet.id).balance == Decimal("0")
    assert result.satisfied is False
    assert result.granted == Decimal("2")
    assert transaction_count(store) == 1


def test_the_most_favourable_modifier_wins(store, alice, provider):
    wallet = open_wallet(store, alice, balance="10")
    add_modifier(store, subject=alice, feature="send_email", factor="0.5")
    add_modifier(store, subject=alice, feature="send_email", factor="0.8")

    result = fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert result.modifier_applied == Decimal("0.8")
    assert store.get_wallet(wallet.id).balance == Decimal("6")  # 10 - 5 × 0.8


def test_an_expired_modifier_is_ignored(store, alice, provider):
    wallet = open_wallet(store, alice, balance="10")
    now = utcnow()
    add_modifier(store, subject=alice, feature="send_email", factor="0.1", valid_until=now - timedelta(days=1))

    result = fulfill(store, "send_email", 5, {"device": alice}, provider, at=now)

    assert result.modifier_applied is None
    assert store.get_wallet(wallet.id).balance == Decimal("5")


def test_a_modifier_for_a_different_feature_is_ignored(store, alice, provider):
    wallet = open_wallet(store, alice, balance="10")
    add_modifier(store, subject=alice, feature="something_else", factor="0.1")

    fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert store.get_wallet(wallet.id).balance == Decimal("5")


def test_floor_rounding_charges_less_and_grants_less(store, alice):
    wallet = open_wallet(store, alice, balance="10")
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

    result = fulfill(store, "send_email", "2.7", {"device": alice}, provider, allow_partial=True)

    assert store.get_wallet(wallet.id).balance == Decimal("8")
    assert result.granted == Decimal("2")
    assert result.satisfied is False


def test_ceil_rounding_charges_a_whole_unit_for_a_fractional_cost(store, alice):
    wallet = open_wallet(store, alice, currency="tokens", balance="5")
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": [("tokens", "0.3")]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"tokens": {"round_fn": "ceil"}},
    )

    result = fulfill(store, "send_email", 1, {"device": alice}, provider)

    # A raw cost of 0.3 is charged as a full token.
    assert store.get_wallet(wallet.id).balance == Decimal("4")
    assert result.satisfied is True
    assert result.granted == Decimal("1")


def test_half_up_rounding_charges_more_than_requested_without_granting_more(store, alice):
    wallet = open_wallet(store, alice, balance="10")
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

    result = fulfill(store, "send_email", "2.5", {"device": alice}, provider)

    assert store.get_wallet(wallet.id).balance == Decimal("7")  # 2.5 rounded up to 3 charged
    assert result.granted == Decimal("2.5")  # but only what was asked for is granted
    assert result.satisfied is True


def test_rounding_never_pushes_a_draw_past_what_the_wallet_holds(store, alice):
    wallet = open_wallet(store, alice, balance="2", overdraft_limit=Decimal("0"))
    provider = provider_with(
        features={
            "send_email": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"device"},
                "fulfillment_order": [("consumable", "device")],
            }
        },
        currencies={"credits": {"general": True, "round_fn": "ceil"}},
    )

    result = fulfill(store, "send_email", "2.5", {"device": alice}, provider, allow_partial=True)

    # Ceiling 2.5 would charge 3 against a balance of 2 — clamped rather than overdrawn.
    assert store.get_wallet(wallet.id).balance == Decimal("0")
    assert result.granted == Decimal("2")


def test_expired_wallets_are_invisible(store, alice, provider):
    now = utcnow()
    expired = open_wallet(store, alice, balance="10", valid_until=now - timedelta(days=1))
    active = open_wallet(store, alice, balance="4")

    result = fulfill(store, "send_email", 3, {"device": alice}, provider, at=now)

    assert store.get_wallet(expired.id).balance == Decimal("10")
    assert store.get_wallet(active.id).balance == Decimal("1")
    assert result.satisfied is True


def test_wallets_not_yet_valid_are_invisible(store, alice, provider):
    now = utcnow()
    future = open_wallet(store, alice, balance="10", valid_from=now + timedelta(days=1))

    with pytest.raises(InsufficientBalanceError):
        fulfill(store, "send_email", 1, {"device": alice}, provider, at=now)

    assert store.get_wallet(future.id).balance == Decimal("10")


def test_a_wallet_in_a_currency_the_feature_does_not_accept_is_skipped(store, alice, provider):
    other = open_wallet(store, alice, currency="coolant_liters", balance="100")
    usable = open_wallet(store, alice, currency="credits", balance="4", overdraft_limit=Decimal("0"))

    result = fulfill(store, "send_email", 4, {"device": alice}, provider)

    assert store.get_wallet(other.id).balance == Decimal("100")
    assert store.get_wallet(usable.id).balance == Decimal("0")
    assert result.satisfied is True


def test_a_supplied_guid_groups_several_calls_into_one_record(store, alice, provider):
    open_wallet(store, alice, balance="10")
    guid = uuid.uuid4()

    first = fulfill(store, "send_email", 2, {"device": alice}, provider, guid=guid)
    second = fulfill(store, "send_email", 3, {"device": alice}, provider, guid=guid)

    assert first.guid == guid
    assert second.guid == guid
    assert len(store.transactions_for_record(record_guid=guid)) == 2


def test_each_call_without_a_guid_gets_its_own_record(store, alice, provider):
    open_wallet(store, alice, balance="10")

    first = fulfill(store, "send_email", 2, {"device": alice}, provider)
    second = fulfill(store, "send_email", 2, {"device": alice}, provider)

    assert first.guid != second.guid


def test_no_subjects_means_nothing_can_be_drawn(store, alice, provider):
    open_wallet(store, alice, balance="10")

    with pytest.raises(InsufficientBalanceError):
        fulfill(store, "send_email", 1, {}, provider)
