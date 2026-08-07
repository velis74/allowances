"""``peek`` — the same planning walk as ``fulfill``, with nothing written."""

from decimal import Decimal

from allowances.engine import fulfill, peek
from tests.conftest import open_wallet
from tests.helpers import transaction_count


def test_peek_changes_nothing(store, alice, provider):
    wallet = open_wallet(store, alice, balance="10")

    result = peek(store, "send_email", 4, {"device": alice}, provider)

    assert store.get_wallet(wallet.id).balance == Decimal("10")
    assert transaction_count(store) == 0
    assert result.transactions == []
    assert result.guid is None
    assert result.granted == Decimal("4")
    assert result.satisfied is True


def test_peek_reports_partial_availability(store, alice, provider):
    open_wallet(store, alice, balance="3")

    result = peek(store, "send_email", 10, {"device": alice}, provider)

    assert result.granted == Decimal("3")
    assert result.satisfied is False
    assert transaction_count(store) == 0


def test_peek_agrees_with_what_fulfill_then_grants(store, alice, provider):
    open_wallet(store, alice, balance="6")

    peeked = peek(store, "send_email", 5, {"device": alice}, provider)
    fulfilled = fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert peeked.granted == fulfilled.granted
    assert peeked.satisfied == fulfilled.satisfied


def test_peek_creates_no_fulfillment_record_even_when_it_fails(store, alice, provider):
    open_wallet(store, alice, balance="1")

    result = peek(store, "send_email", 100, {"device": alice}, provider)

    assert result.guid is None
    assert result.satisfied is False


def test_peek_reports_the_modifier_it_would_apply(store, alice, provider):
    from tests.helpers import add_modifier

    open_wallet(store, alice, balance="10")
    add_modifier(store, subject=alice, feature="send_email", factor="0.5")

    result = peek(store, "send_email", 4, {"device": alice}, provider)

    assert result.modifier_applied == Decimal("0.5")
    assert result.satisfied is True
