"""
Translating between Django model instances and the core's :class:`~allowances.types.SubjectRef`.

Application code passes real objects — a ``User``, an ``Organisation``. The core only ever handles
``("auth.user", "42")``. This module is the seam.
"""

from __future__ import annotations

from django.apps import apps
from django.db.models import Model

from allowances.types import ObjectRef, SubjectRef


def subject_ref(obj) -> SubjectRef:
    """
    A :class:`~allowances.types.SubjectRef` for a model instance. Already-converted refs pass
    through, so callers can mix the two freely.
    """
    if isinstance(obj, SubjectRef):
        return obj
    if not isinstance(obj, Model):
        raise TypeError(f"Expected a Django model instance or a SubjectRef, got {type(obj).__name__}")
    if obj.pk is None:
        raise ValueError(f"Cannot reference an unsaved {type(obj).__name__} — it has no primary key yet")
    return SubjectRef(f"{obj._meta.app_label}.{obj._meta.model_name}", obj.pk)


def object_ref(obj) -> ObjectRef:
    """The same conversion for the domain object a fulfillment paid for."""
    if isinstance(obj, ObjectRef):
        return obj
    if not isinstance(obj, Model):
        raise TypeError(f"Expected a Django model instance or an ObjectRef, got {type(obj).__name__}")
    if obj.pk is None:
        raise ValueError(f"Cannot reference an unsaved {type(obj).__name__} — it has no primary key yet")
    return ObjectRef(f"{obj._meta.app_label}.{obj._meta.model_name}", obj.pk)


def subject_refs(subjects: dict | None) -> dict:
    """Convert a whole ``{name: instance}`` mapping in one go."""
    return {name: subject_ref(obj) for name, obj in (subjects or {}).items()}


def resolve(ref: SubjectRef | ObjectRef):
    """
    Fetch the actual object behind a reference, or ``None`` if it is gone.

    Since these references are not database-enforced, a subject really can be deleted out from
    under its wallets. Returning ``None`` rather than raising lets reporting and admin views render
    an orphaned row instead of falling over on it.
    """
    app_label, _, model_name = ref.type.partition(".")
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return None
    return model.objects.filter(pk=ref.id).first()
