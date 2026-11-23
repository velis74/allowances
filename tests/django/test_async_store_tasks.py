"""
Tasks created inside a unit of work of the asynchronous ``allowances_django.store.DjangoStore`` share its
transaction; their nested blocks are savepoints that run one after another.
"""

import asyncio

from decimal import Decimal

import pytest

from asgiref.sync import sync_to_async

from allowances.types import SubjectRef, WalletType
from allowances_django.models import Wallet as WalletRow
from allowances_django.store import DjangoStore

pytestmark = pytest.mark.django_db(transaction=True)


class Boom(Exception):
    pass


async def create(store, subject_id):
    return await store.create_wallet(
        subject=SubjectRef("auth.user", subject_id),
        wallet_type=WalletType.CONSUMABLE,
        currency="credits",
        balance=Decimal("1"),
    )


async def subjects():
    return sorted(await sync_to_async(list)(WalletRow.objects.values_list("subject_id", flat=True)))


async def test_a_failing_nested_block_of_one_task_keeps_the_nested_writes_of_another():
    store = DjangoStore()

    async def failing():
        with pytest.raises(Boom):
            async with store.unit_of_work():
                await create(store, "1")
                await asyncio.sleep(0.05)
                raise Boom

    async def succeeding():
        await asyncio.sleep(0.01)
        async with store.unit_of_work():
            await create(store, "2")

    async with store.unit_of_work():
        await asyncio.gather(failing(), succeeding())

    assert await subjects() == ["2"]


async def test_tasks_inside_a_unit_of_work_share_its_transaction():
    store = DjangoStore()

    async with store.unit_of_work():
        first, _ = await asyncio.gather(create(store, "1"), create(store, "2"))
        assert (await store.get_wallet(first.id)).subject == SubjectRef("auth.user", "1")

    assert await subjects() == ["1", "2"]


async def test_a_failing_unit_of_work_rolls_back_the_writes_of_its_tasks():
    store = DjangoStore()

    with pytest.raises(Boom):
        async with store.unit_of_work():
            await asyncio.gather(create(store, "1"), create(store, "2"))
            raise Boom

    assert await subjects() == []


async def test_an_operation_of_another_task_waits_for_an_open_nested_block():
    store = DjangoStore()
    order = []

    async def nested():
        async with store.unit_of_work():
            await create(store, "1")
            await asyncio.sleep(0.05)
            order.append("nested block ends")
            raise Boom

    async def outer_write():
        await asyncio.sleep(0.01)
        await create(store, "2")
        order.append("outer write")

    async with store.unit_of_work():
        results = await asyncio.gather(nested(), outer_write(), return_exceptions=True)

    assert isinstance(results[0], Boom)
    assert order == ["nested block ends", "outer write"]
    assert await subjects() == ["2"]
