"""Parsing the definition dict. Pure, no store."""

from decimal import Decimal

import pytest

from allowances.definitions import CurrencySpec, DictProvider
from allowances.errors import DefinitionNotFoundError

CONFIG = {
    "features": {
        "send_email": {
            "grant": {"currencies": ["email_send"]},
            "consumable": {"currencies": ["email_send", ("credits", 0.5)]},
            "subjects": {"device", "team", "location"},
            "fulfillment_order": [("grant", "device"), ("grant", None), "location", "team"],
            "overdraft": False,
        },
        "coolant_fill": {
            "consumable": {"currencies": ["coolant_liters", ("credits", 3.27)]},
            "subjects": {"device", "team"},
            "fulfillment_order": [("grant", None), "device", "team"],
        },
    },
    "currencies": {
        "email_send": {"general": False, "round_fn": "floor"},
        "credits": {"general": True, "round_fn": None},
        "coolant_liters": {},
    },
}


@pytest.fixture
def provider():
    return DictProvider(CONFIG)


def test_parses_both_feature_sections(provider):
    feature = provider.get_feature("send_email")

    assert feature.name == "send_email"
    assert feature.grant.currencies == (CurrencySpec(currency="email_send", rate=Decimal("1")),)
    assert feature.consumable.currencies == (
        CurrencySpec(currency="email_send", rate=Decimal("1")),
        CurrencySpec(currency="credits", rate=Decimal("0.5")),
    )
    assert feature.subjects == frozenset({"device", "team", "location"})
    assert feature.overdraft is False


def test_a_missing_section_is_none_not_empty(provider):
    assert provider.get_feature("coolant_fill").grant is None


def test_conversion_rates_survive_as_exact_decimals(provider):
    # 3.27 is not representable as a float; going through str() keeps it exact.
    assert provider.get_feature("coolant_fill").consumable.rate_for("credits") == Decimal("3.27")


def test_a_bare_string_currency_means_rate_one(provider):
    assert provider.get_feature("send_email").consumable.rate_for("email_send") == Decimal("1")


def test_rate_for_an_unaccepted_currency_is_none(provider):
    assert provider.get_feature("send_email").consumable.rate_for("coolant_liters") is None


def test_fulfillment_order_shorthand_expands_to_a_wildcard_wallet_type(provider):
    assert provider.get_feature("send_email").fulfillment_order == [
        ("grant", "device"),
        ("grant", None),
        (None, "location"),
        (None, "team"),
    ]


def test_dotted_string_fulfillment_order_is_imported():
    provider = DictProvider(
        {
            "features": {
                "custom": {
                    "consumable": {"currencies": ["credits"]},
                    "subjects": {"team"},
                    "fulfillment_order": "tests.fixtures_ordering.order_wallets",
                }
            },
            "currencies": {"credits": {}},
        }
    )

    assert callable(provider.get_feature("custom").fulfillment_order)


def test_a_callable_may_be_passed_directly_without_a_dotted_path():
    def order(wallets):
        return wallets

    provider = DictProvider(
        {
            "features": {"custom": {"consumable": {"currencies": ["c"]}, "fulfillment_order": order}},
            "currencies": {"c": {}},
        }
    )

    assert provider.get_feature("custom").fulfillment_order is order


def test_currency_defaults_are_not_general_and_not_rounded(provider):
    currency = provider.get_currency("coolant_liters")

    assert currency.general is False
    assert currency.round_fn is None


def test_currency_flags_are_read(provider):
    currency = provider.get_currency("credits")

    assert currency.general is True
    assert currency.round_fn is None


def test_unknown_feature_raises(provider):
    with pytest.raises(DefinitionNotFoundError):
        provider.get_feature("does_not_exist")


def test_unknown_currency_raises(provider):
    with pytest.raises(DefinitionNotFoundError):
        provider.get_currency("does_not_exist")


def test_an_empty_provider_still_raises_rather_than_returning_none():
    with pytest.raises(DefinitionNotFoundError):
        DictProvider().get_feature("anything")
