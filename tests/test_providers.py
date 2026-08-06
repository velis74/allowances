from decimal import Decimal

import pytest

from django.test import override_settings

from django_licensing.providers import (
    CurrencySpec,
    DefinitionNotFoundError,
    get_default_provider,
    SettingsProvider,
)

LICENSING_CONFIG = {
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
            "overdraft": False,
        },
    },
    "currencies": {
        "email_send": {"general": False, "round_fn": "floor"},
        "credits": {"general": True, "round_fn": None},
        "coolant_liters": {"general": False, "round_fn": None},
    },
}


def test_get_feature_parses_grant_and_consumable_sections():
    provider = SettingsProvider(config=LICENSING_CONFIG)

    feature = provider.get_feature("send_email")

    assert feature.name == "send_email"
    assert feature.grant.currencies == (CurrencySpec(currency="email_send", rate=Decimal("1")),)
    assert feature.consumable.currencies == (
        CurrencySpec(currency="email_send", rate=Decimal("1")),
        CurrencySpec(currency="credits", rate=Decimal("0.5")),
    )
    assert feature.subjects == frozenset({"device", "team", "location"})
    assert feature.overdraft is False


def test_get_feature_parses_currency_tuple_conversion_rate():
    provider = SettingsProvider(config=LICENSING_CONFIG)

    feature = provider.get_feature("coolant_fill")

    assert feature.grant is None
    rate = feature.consumable.rate_for("credits")
    assert rate == Decimal("3.27")


def test_get_feature_parses_fulfillment_order_tuples_and_shorthand():
    provider = SettingsProvider(config=LICENSING_CONFIG)

    feature = provider.get_feature("send_email")

    assert feature.fulfillment_order == [
        ("grant", "device"),
        ("grant", None),
        (None, "location"),
        (None, "team"),
    ]


def test_get_feature_parses_dotted_string_fulfillment_order_as_callable():
    config = {
        "features": {
            "custom": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"team"},
                "fulfillment_order": "tests.fixtures_ordering.order_wallets",
            }
        },
        "currencies": {"credits": {"general": True, "round_fn": None}},
    }
    provider = SettingsProvider(config=config)

    feature = provider.get_feature("custom")

    assert callable(feature.fulfillment_order)


def test_get_currency_parses_general_and_round_fn():
    provider = SettingsProvider(config=LICENSING_CONFIG)

    currency = provider.get_currency("credits")

    assert currency.name == "credits"
    assert currency.general is True
    assert currency.round_fn is None


def test_get_feature_raises_for_unknown_feature():
    provider = SettingsProvider(config=LICENSING_CONFIG)

    with pytest.raises(DefinitionNotFoundError):
        provider.get_feature("does_not_exist")


def test_get_currency_raises_for_unknown_currency():
    provider = SettingsProvider(config=LICENSING_CONFIG)

    with pytest.raises(DefinitionNotFoundError):
        provider.get_currency("does_not_exist")


@override_settings(LICENSING=LICENSING_CONFIG)
def test_settings_provider_reads_from_django_settings_when_no_config_injected():
    provider = SettingsProvider()

    feature = provider.get_feature("send_email")

    assert feature.name == "send_email"


@override_settings(LICENSING=LICENSING_CONFIG)
def test_get_default_provider_reads_settings():
    provider = get_default_provider()

    feature = provider.get_feature("coolant_fill")

    assert feature.name == "coolant_fill"
