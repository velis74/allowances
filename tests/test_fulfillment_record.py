import uuid

import pytest

from django_licensing.models import FulfillmentRecord

pytestmark = pytest.mark.django_db


def test_guid_generated_automatically_when_not_provided():
    record = FulfillmentRecord.objects.create()

    assert record.guid is not None
    assert isinstance(record.guid, uuid.UUID)


def test_two_records_get_different_auto_generated_guids():
    first = FulfillmentRecord.objects.create()
    second = FulfillmentRecord.objects.create()

    assert first.guid != second.guid


def test_guid_can_be_supplied_explicitly_to_group_multiple_fulfill_calls():
    guid = uuid.uuid4()

    record = FulfillmentRecord.objects.create(guid=guid)

    assert record.guid == guid


def test_guid_is_unique():
    guid = uuid.uuid4()
    FulfillmentRecord.objects.create(guid=guid)

    with pytest.raises(Exception):
        FulfillmentRecord.objects.create(guid=guid)


def test_timestamps_are_set():
    record = FulfillmentRecord.objects.create()

    assert record.created_at is not None
    assert record.updated_at is not None
