"""
Conversion between Django model instances and the core's :class:`~allowances.types.SubjectRef` /
:class:`~allowances.types.ObjectRef` (``("auth.user", "42")``), and back.
"""

from __future__ import annotations

from django.apps import apps
from django.db.models import Model

from allowances.types import ObjectRef, SubjectRef


def subject_ref(obj) -> SubjectRef:
    """
    A :class:`~allowances.types.SubjectRef` for a model instance. A ``SubjectRef`` is returned
    unchanged.
    """
    if isinstance(obj, SubjectRef):
        return obj
    if not isinstance(obj, Model):
        raise TypeError(f"Expected a Django model instance or a SubjectRef, got {type(obj).__name__}")
    if obj.pk is None:
        raise ValueError(f"Cannot reference an unsaved {type(obj).__name__} — it has no primary key yet")
    return SubjectRef(f"{obj._meta.app_label}.{obj._meta.model_name}", obj.pk)


def object_ref(obj) -> ObjectRef:
    """
    An :class:`~allowances.types.ObjectRef` for the domain object a fulfilment paid for. An
    ``ObjectRef`` is returned unchanged.
    """
    if isinstance(obj, ObjectRef):
        return obj
    if not isinstance(obj, Model):
        raise TypeError(f"Expected a Django model instance or an ObjectRef, got {type(obj).__name__}")
    if obj.pk is None:
        raise ValueError(f"Cannot reference an unsaved {type(obj).__name__} — it has no primary key yet")
    return ObjectRef(f"{obj._meta.app_label}.{obj._meta.model_name}", obj.pk)


def subject_refs(subjects: dict | None) -> dict:
    """Convert a ``{name: instance}`` mapping with :func:`subject_ref`."""
    return {name: subject_ref(obj) for name, obj in (subjects or {}).items()}


def resolve(ref: SubjectRef | ObjectRef):
    """
    The object behind a reference, or ``None`` when its model or row does not exist.

    References are not foreign keys: a subject can be deleted while its wallets remain.
    """
    app_label, _, model_name = ref.type.partition(".")
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return None
    return model.objects.filter(pk=ref.id).first()
