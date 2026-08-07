"""Archival: wallets leave live storage once they can no longer pay for anything."""

from datetime import timedelta
from decimal import Decimal

from allowances.engine import archive_expired_wallets, fulfill
from allowances.windows import utcnow
from tests.conftest import CREDITS, open_wallet, provider_with
from tests.helpers import archived_transaction_amounts, archived_wallet_balance, archived_wallet_ids


def test_a_wallet_drained_to_zero_is_archived_immediately(store, alice, provider):
    wallet = open_wallet(store, alice, balance="5")

    fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert store.get_wallet(wallet.id) is None
    assert archived_wallet_ids(store) == [wallet.id]
    assert archived_wallet_balance(store, wallet.id) == Decimal("0")
    assert archived_transaction_amounts(store, wallet.id) == [Decimal("-5")]


def test_a_wallet_with_balance_left_stays_put(store, alice, provider):
    wallet = open_wallet(store, alice, balance="10")

    fulfill(store, "send_email", 5, {"device": alice}, provider)

    assert store.get_wallet(wallet.id) is not None
    assert archived_wallet_ids(store) == []


def test_a_drained_wallet_with_overdraft_headroom_stays_put(store, alice):
    wallet = open_wallet(store, alice, balance="5", overdraft_limit=Decimal("10"))
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

    fulfill(store, "send_email", 5, {"device": alice}, provider)

    # Zero balance but still able to fund the next request, so it is not spent out.
    assert store.get_wallet(wallet.id) is not None
    assert archived_wallet_ids(store) == []


def test_expired_wallets_are_swept_up_even_though_nothing_ever_touched_them(store, alice):
    now = utcnow()
    wallet = open_wallet(store, alice, balance="7", valid_until=now - timedelta(days=1))

    archived = archive_expired_wallets(store, at=now)

    assert archived == 1
    assert store.get_wallet(wallet.id) is None
    assert archived_wallet_balance(store, wallet.id) == Decimal("7")


def test_the_sweep_leaves_active_wallets_alone(store, alice):
    wallet = open_wallet(store, alice, balance="7")

    assert archive_expired_wallets(store) == 0
    assert store.get_wallet(wallet.id) is not None


def test_the_sweep_leaves_wallets_that_expire_in_the_future_alone(store, alice):
    now = utcnow()
    wallet = open_wallet(store, alice, balance="7", valid_until=now + timedelta(days=1))

    assert archive_expired_wallets(store, at=now) == 0
    assert store.get_wallet(wallet.id) is not None


def test_archiving_carries_the_whole_transaction_history_along(store, alice, provider):
    from dataclasses import replace

    from allowances.engine import unfulfill

    now = utcnow()
    # Balance stays positive throughout, so the drained trigger never fires — this wallet only
    # leaves live storage because it is expired below.
    wallet = open_wallet(store, alice, balance="10")
    result = fulfill(store, "send_email", 3, {"device": alice}, provider)
    unfulfill(store, result.guid)

    store.save_wallet(replace(store.get_wallet(wallet.id), valid_until=now - timedelta(seconds=1)))
    archived = archive_expired_wallets(store, at=now)

    assert archived == 1
    assert sorted(archived_transaction_amounts(store, wallet.id)) == [Decimal("-3"), Decimal("3")]
