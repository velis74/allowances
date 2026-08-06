from decimal import Decimal

import pytest

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from django_licensing.models import LicenseModifier

pytestmark = pytest.mark.django_db


@pytest.fixture
def subject():
    return User.objects.create(username="carol")


def test_create_license_modifier(subject):
    modifier = LicenseModifier.objects.create(subject=subject, feature="send_email", factor=Decimal("0.5"))
    modifier.refresh_from_db()

    assert modifier.feature == "send_email"
    assert modifier.factor == Decimal("0.500000")
    assert modifier.valid_from is None
    assert modifier.valid_until is None
    assert modifier.created_at is not None


def test_license_modifier_subject_generic_foreign_key(subject):
    modifier = LicenseModifier.objects.create(subject=subject, feature="coolant_fill", factor=Decimal("3.27"))
    modifier.refresh_from_db()

    assert modifier.subject == subject
    assert modifier.subject_content_type == ContentType.objects.get_for_model(User)
    assert modifier.subject_object_id == subject.pk


def test_license_modifier_factor_accepts_readme_example_values(subject):
    modifier = LicenseModifier.objects.create(subject=subject, feature="coolant_fill", factor=Decimal("3.27"))
    modifier.refresh_from_db()

    assert modifier.factor == Decimal("3.270000")


def test_license_modifier_validity_window(subject):
    valid_from = timezone.now()
    valid_until = valid_from + timezone.timedelta(days=7)

    modifier = LicenseModifier.objects.create(
        subject=subject,
        feature="send_email",
        factor=Decimal("0.5"),
        valid_from=valid_from,
        valid_until=valid_until,
    )
    modifier.refresh_from_db()

    assert modifier.valid_from == valid_from
    assert modifier.valid_until == valid_until


def test_multiple_modifiers_can_apply_to_same_subject_and_feature(subject):
    LicenseModifier.objects.create(subject=subject, feature="send_email", factor=Decimal("0.5"))
    LicenseModifier.objects.create(subject=subject, feature="send_email", factor=Decimal("0.8"))

    assert LicenseModifier.objects.filter(feature="send_email").count() == 2
