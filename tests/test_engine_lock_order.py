"""
Every engine operation that locks wallets locks all of them in one ``lock_wallets`` call and takes
no other wallet lock.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from allowances.backends import MemoryStore
from allowances.types import SubjectRef, SwitchPolicy, WalletSpec, WalletType
from allowances.windows import utcnow
from tests.api import (
    archive_expired_wallets,
    cancel_scheduled_switch,
    fulfill,
    plan_switch,
    transfer,
    unfulfill,
)
from tests.conftest import CREDITS, open_wallet, provider_with

USER = SubjectRef("auth.user", 1)
TEAM = SubjectRef("team", 1)


class RecordingStore(MemoryStore):
    """Records every wallet lock the engine asks for."""

    def __init__(self):
        super().__init__()
        self.locks: list = []

    async def lock_wallets(self, wallet_ids):
        self.locks.append(sorted(wallet_ids))
        return await super().lock_wallets(wallet_ids)

    async def find_wallets(self, *, lock=False, **kwargs):
        if lock:
            self.locks.append(("find_wallets",))
        return await super().find_wallets(**kwargs)

    async def find_scheduled_wallets(self, *, subject, lock=False):
        if lock:
            self.locks.append(("find_scheduled_wallets",))
        return await super().find_scheduled_wallets(subject=subject)

    async def find_expired_wallets(self, *, at, lock=False):
        if lock:
            self.locks.append(("find_expired_wallets",))
        return await super().find_expired_wallets(at=at)

    async def get_wallet(self, wallet_id, *, lock=False):
        if lock:
            self.locks.append(("get_wallet", wallet_id))
        return await super().get_wallet(wallet_id)


@pytest.fixture
def store():
    return RecordingStore()


@pytest.fixture
def provider():
    return provider_with(
        features={
            "team_first": {
                "consumable": {"currencies": ["credits"]},
                "subjects": {"user", "team"},
                "fulfillment_order": [("consumable", "team"), ("consumable", "user")],
            }
        },
        currencies=CREDITS,
    )


async def test_fulfill_locks_every_candidate_in_one_call(store, provider):
    user = await open_wallet(store, USER, balance="10")
    team = await open_wallet(store, TEAM, balance="1", overdraft_limit=Decimal("0"))

    await fulfill(store, "team_first", 3, {"user": USER, "team": TEAM}, provider)

    assert store.locks == [sorted([user.id, team.id])]


async def test_unfulfill_locks_every_wallet_of_the_record_in_one_call(store, provider):
    user = await open_wallet(store, USER, balance="10")
    team = await open_wallet(store, TEAM, balance="1", overdraft_limit=Decimal("0"))
    result = await fulfill(store, "team_first", 3, {"user": USER, "team": TEAM}, provider)
    store.locks.clear()

    await unfulfill(store, result.guid)

    assert store.locks == [sorted([user.id, team.id])]


async def test_transfer_locks_both_wallets_in_one_call(store):
    source = await open_wallet(store, USER, balance="10")
    target = await open_wallet(store, TEAM, balance="0")

    await transfer(store, source.id, target.id, "1")

    assert store.locks == [sorted([source.id, target.id])]


async def test_plan_switch_locks_every_wallet_of_every_subject_in_one_call(store):
    first = await open_wallet(store, USER, balance="10")
    second = await open_wallet(store, TEAM, balance="10", wallet_type=WalletType.GRANT)

    await plan_switch(store, {"user": USER, "team": TEAM}, [WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("5"))])

    assert store.locks == [sorted([first.id, second.id])]


async def test_cancel_scheduled_switch_locks_in_one_call(store):
    spec = WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("5"), on_switch=SwitchPolicy.TOP_UP)
    await plan_switch(store, {"user": USER, "team": TEAM}, [spec])
    await plan_switch(store, {"user": USER, "team": TEAM}, [spec], effective_date=utcnow() + timedelta(days=1))
    store.locks.clear()

    assert await cancel_scheduled_switch(store, {"user": USER, "team": TEAM}) == 2

    assert len(store.locks) == 1


async def test_archive_expired_wallets_locks_in_one_call(store):
    past = utcnow() - timedelta(seconds=1)
    first = await open_wallet(store, USER, valid_until=past)
    second = await open_wallet(store, TEAM, valid_until=past)

    assert await archive_expired_wallets(store) == 2

    assert store.locks == [sorted([first.id, second.id])]
