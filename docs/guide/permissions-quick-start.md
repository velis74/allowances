---
description: 'Set up Django permissions served by allowances: the authentication backend, groups through membership, granting, temporary and revoked rights, where a right comes from, and audited use.'
---

# Permissions quick start

This page sets up a Django project whose permission checks — `user.has_perm`, `@permission_required`, `{% if perms.…
%}`, DRF permission classes — are answered from allowances. The examples assume an application `club` with a model
`Member`, so Django has the permission `club.change_member`. [Rights and permissions](/guide/permissions) explains the
model behind it and [when to choose it over Django's own
permissions](/guide/permissions#allowances-or-django-contrib-auth).

## Install

```bash
pip install "allowances[django]"
```

```python
# settings.py
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    # ...
    "allowances_django",
    "club",
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allowances_django.permissions.AllowancesBackend",
]
```

```bash
python manage.py migrate
```

With both backends a user holds a permission that either of them grants: Django's `user_permissions` and
`Group.permissions` keep working next to the rights held in allowances.

`AllowancesBackend` authenticates nobody. To make it the only source of permissions, pair it with a login backend that
answers no permission checks, such as a `ModelBackend` subclass that reads neither `user_permissions` nor
`Group.permissions`:

```python
# accounts/backends.py
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class LoginBackend(ModelBackend):
    def get_user_permissions(self, user_obj, obj=None):
        return set()

    async def aget_user_permissions(self, user_obj, obj=None):
        return set()

    def get_group_permissions(self, user_obj, obj=None):
        return set()

    async def aget_group_permissions(self, user_obj, obj=None):
        return set()

    def with_perm(self, perm, is_active=True, include_superusers=True, obj=None):
        return get_user_model()._default_manager.none()
```

`get_all_permissions`, `has_perm`, `has_module_perms` and their async variants build on the first four methods, so they
return nothing from this backend either.

```python
AUTHENTICATION_BACKENDS = [
    "accounts.backends.LoginBackend",
    "allowances_django.permissions.AllowancesBackend",
]
```

No `ALLOWANCES` setting is needed: every permission `"app_label.codename"` is served as the feature and currency
`"perm:app_label.codename"`.

::: tip SQLite
On SQLite set `DATABASES["default"]["OPTIONS"]["transaction_mode"] = "IMMEDIATE"`. The system check `allowances.W001`
warns when it is missing; see [SQLite](/backends/django#sqlite).
:::

## Grant a permission to a group

A group is any model instance; here it is Django's `auth.Group`. Membership is recorded with `add_member`:
`AllowancesBackend` reads allowances memberships only, and `user.groups` counts only for `ModelBackend`.

```python
from django.contrib.auth.models import Group, User

from allowances_django.permissions import grant_permission
from allowances_django.shortcuts import add_member

editors = Group.objects.create(name="editors")
user = User.objects.get(username="ana")

add_member(user, editors)
grant_permission(editors, "club.change_member")

User.objects.get(pk=user.pk).has_perm("club.change_member")   # True
```

The backend caches a user's permission set on the user object, as `ModelBackend` does. A grant or revocation takes
effect for the next loaded user object, which each request loads anew.

## Check it

Everything that calls `has_perm` reads the right.

```python
# views.py
from django.contrib.auth.decorators import permission_required


@permission_required("club.change_member", raise_exception=True)
def edit_member(request, pk):
    ...


def member_detail(request, pk):
    can_edit = request.user.has_perm("club.change_member")
    ...
```

```html
{% if perms.club.change_member %}
  <a href="{% url 'edit_member' member.pk %}">Edit</a>
{% endif %}
```

```python
# Django REST framework
from rest_framework import viewsets
from rest_framework.permissions import BasePermission, DjangoModelPermissions

from club.models import Member
from club.serializers import MemberSerializer


class MemberViewSet(viewsets.ModelViewSet):
    queryset = Member.objects.all()
    serializer_class = MemberSerializer
    permission_classes = [DjangoModelPermissions]   # PUT and PATCH need club.change_member


class CanEditMembers(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("club.change_member")
```

## A temporary grant

```python
from datetime import timedelta

from django.utils import timezone

grant_permission(user, "club.view_report", valid_until=timezone.now() + timedelta(days=30))
```

The permission is not held after `valid_until`; no scheduled job is needed. `valid_from=` starts a grant later.

## Nested groups

Groups may be members of groups, to any depth. A department inside an organisation:

```python
organisation = Group.objects.create(name="organisation")
department = Group.objects.create(name="department")

add_member(department, organisation)
add_member(user, department)
grant_permission(organisation, "club.view_member")

User.objects.get(pk=user.pk).has_perm("club.view_member")   # True, through two levels
```

`add_member` raises `MembershipCycleError` for a membership that would form a cycle. The group need not be an
`auth.Group`: a `Club`, an `Organisation` or a `Device` model works the same way.

## Where a right comes from

`effective_allowances` lists every wallet a subject holds, its own and its groups'; `effective_permissions` keeps the
ones that are Django permissions and groups them by permission.

```python
from allowances_django.permissions import effective_permissions
from allowances_django.shortcuts import effective_allowances

effective_permissions(effective_allowances(user))
# {
#     "club.change_member": [
#         Allowance(wallet=Wallet(id=7, ...), holder=SubjectRef("auth.group", "1"),
#                   depth=1, balance=Decimal("1.000000")),
#     ],
#     "club.view_member": [
#         Allowance(wallet=Wallet(id=9, ...), holder=SubjectRef("auth.group", "2"),
#                   depth=2, balance=Decimal("1.000000")),
#     ],
#     "club.view_report": [
#         Allowance(wallet=Wallet(id=8, ..., valid_until=datetime(2026, 12, 23, ...)),
#                   holder=SubjectRef("auth.user", "5"), depth=0, balance=Decimal("1.000000")),
#     ],
# }
```

`holder` is the subject whose wallet confers the permission and `depth` how far it is from the user: 0 for the user's
own grant, 1 for a direct group, 2 for a group of that group. `effective_allowances` takes any subject, not only users —
`effective_allowances(department)` shows what the department holds. [Effective
permissions](/guide/permissions#effective-permissions) describes the result in full.

## Record each use

`has_perm` checks a right and writes nothing. To keep a record of who used it and on what, call `fulfill` with the
permission's feature and attach the object:

```python
from django.core.exceptions import PermissionDenied

from allowances import InsufficientBalanceError
from allowances_django.permissions import permission_feature
from allowances_django.shortcuts import attach, fulfill, unfulfill


def update_member(request, member):
    try:
        result = fulfill(permission_feature("club.change_member"), 1, {"user": request.user})
    except InsufficientBalanceError:
        raise PermissionDenied
    try:
        member.save()
    except Exception:
        unfulfill(result.guid)
        raise
    attach(result.guid, member)
```

The entry goes on the wallet that holds the right — the `editors` group's — with `request.user` as its actor,
at amount 0. `fulfill` resolves the user and its groups like the backend, but does not apply the superuser rule.
See [The actor](/guide/permissions#the-actor).

## Revoke

```python
from allowances_django.permissions import revoke_permission
from allowances_django.shortcuts import remove_member

revoke_permission(editors, "club.change_member")   # 1: the number of wallets closed
remove_member(user, department)                    # True
```

Every member of `editors` loses the permission at once, unless it holds it another way. `remove_member` removes one
member's access to everything the group holds.
