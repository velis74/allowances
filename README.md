# allowances

Grants and consumable budgets, checked before an operation and recorded in an append-only ledger. Storage-agnostic,
with a Django backend that also serves Django permissions: expiring, inherited through nested groups, held by any model,
with each use recordable on the ledger.

📖 **Documentation:** [docs.velis.si/allowances](https://docs.velis.si/allowances). The source is in `docs/`;
`npm install && npm run docs:dev` serves it locally.

---

## What it does

Typical checks:

- whether a user may send an email now;
- how many API calls an organisation has left this month;
- whether a device has a valid service contract and the customer enough coolant credit to fill it.

`fulfill` checks and consumes the balance and writes a ledger entry; `unfulfill` reverses the entries of an operation
that did not complete.

The same wallets hold rights that are not consumed. In Django they answer `user.has_perm`, `@permission_required` and
`{% if perms.… %}` through an authentication backend; see the
[Permissions quick start](https://docs.velis.si/allowances/guide/permissions-quick-start).

It is not a payment system: it has no invoices or card charges. Units are defined by the application. It covers rights
that are bought, expire or are consumed.

## Install

Django is optional. The core (`import allowances`) uses only the standard library. It implements the fulfilment
order, conversion, rounding, overdraft and sliding windows, and accesses storage through a 22-method store port.

```bash
pip install allowances            # the engine, zero dependencies
pip install "allowances[django]"  # ...and the Django models, store and admin
```

The Django backend is enabled in `INSTALLED_APPS`; the examples below use it:

```python
INSTALLED_APPS = ["allowances_django"]
```

Without it, pass a `Store` to the core functions: the core includes the dict-backed `MemoryStore` for tests, and
[Writing a backend](https://docs.velis.si/allowances/backends/writing-a-backend) describes the port.

## Use

```python
# settings.py
ALLOWANCES = {
    "currencies": {
        "email_send": {},
        "credits": {"general": True, "round_fn": "ceil"},
    },
    "features": {
        "send_email": {
            "consumable": {"currencies": ["email_send", ("credits", 0.5)]},
            "subjects": {"user", "team"},
            "fulfillment_order": [("consumable", "user"), ("consumable", "team")],
        },
    },
}
```

```python
from allowances import InsufficientBalanceError
from allowances_django.shortcuts import fulfill, attach, unfulfill

try:
    result = fulfill("send_email", 1, {"user": request.user, "team": team})
except InsufficientBalanceError as exc:
    return http_402(f"Only {exc.result.granted} left")

message = send_the_email()
attach(result.guid, message)   # link the ledger entries to the sent message
# ...or unfulfill(result.guid) if it failed
```

## Architecture

The library separates two layers. **Definition**: which rights exist, i.e. features, currencies, conversion rates and
priority order; it is configuration. **State**: what a subject holds and has consumed, i.e. wallets and a transaction
ledger.

The engine reads definitions through a `DefinitionProvider` and state through a 22-method store port; it runs unchanged
on Postgres, SQLite or `MemoryStore`.

```text
   allowances.engine        peek · fulfill · unfulfill · transfer · add_member · plan_switch
           │
   ┌───────┴────────┐
   ▼                ▼
Store port   DefinitionProvider
   │                │
   ▼                ▼
DjangoStore    SettingsProvider
MemoryStore    DictProvider
```

Highlights:

- **Grants and consumables in one call.** A boolean entitlement and a metered budget are resolved together, in the
  declared fulfilment order.
- **Several sources.** One operation can draw from a dedicated budget, then general credits, then a promotional grant,
  at configured conversion rates.
- **Append-only ledger.** Consumption writes a ledger entry; reversal writes a matching storno; drained wallets are
  moved to an archive.
- **Sliding windows.** A limit such as "200 a day" is computed from the consumption inside a sliding window; no reset
  job is needed.
- **Several subjects.** A licence can belong to a user, an organisation, a device or a location; one call resolves all
  of them, together with the groups each one is a member of.

## As a permissions layer

`allowances_django.permissions.AllowancesBackend` answers Django's permission checks from grant wallets, so `has_perm`
callers (views, templates, the admin, DRF) use them. Compared with `django.contrib.auth` permissions it adds:

- **Expiring rights.** A grant has `valid_from` and `valid_until` and is not counted outside that range; no cleanup job
  is needed.
- **Nested groups of any model.** Any model can be a group (a department, an organisation, a club), nested to any depth;
  a membership that would form a cycle is rejected.
- **Rights held by any subject.** A device, an organisation or a club membership holds and inherits rights as a user
  does.
- **Bought, plan-bound and countable rights.** A right is a wallet, so `plan_switch` and purchases grant it like a
  quota, and a right can allow *n* uses.
- **Use and origin records.** `fulfill` writes each use to the ledger with its actor; `effective_allowances` lists
  every right and balance with the subject or group it comes from.

```python
# settings.py
INSTALLED_APPS = [..., "allowances_django"]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allowances_django.permissions.AllowancesBackend",
]
```

```python
from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.utils import timezone

from allowances_django.permissions import effective_permissions, grant_permission
from allowances_django.shortcuts import add_member, effective_allowances

user = User.objects.get(username="ana")
editors = Group.objects.create(name="editors")
add_member(user, editors)
grant_permission(editors, "club.change_member", valid_until=timezone.now() + timedelta(days=90))

user.has_perm("club.change_member")   # True (on a freshly loaded user)

effective_permissions(effective_allowances(user))
# {"club.change_member": [Allowance(wallet=Wallet(...), holder=SubjectRef("auth.group", "1"),
#                                   depth=1, balance=Decimal("1.000000"))]}
```

`effective_allowances` returns every wallet the user and its groups hold, budgets and rights alike, as `Allowance`
objects; `effective_permissions` keeps the Django permissions. The backend works alone or chained after
`ModelBackend`. It has no object-level permissions, no deny rules and no admin UI for granting.

- [Permissions quick start](https://docs.velis.si/allowances/guide/permissions-quick-start)
- [Rights and permissions, and how they compare with Django's](https://docs.velis.si/allowances/guide/permissions)

## Development

```bash
uv sync
uv run pytest                          # both stores, no setup
uv run ruff check . && uv run ruff format --check .

npm install
npm run docs:dev
```

The suite uses a file-backed SQLite database by default. `tests/env.py` (copied from `tests/env.example.py`) selects a
local Postgres; `ALLOWANCES_TEST_DB=sqlite` forces SQLite. Both databases are supported.
`tests/django/test_concurrency.py` runs eight threads against one wallet and asserts it is granted exactly once.

Supported: Django 5.2 to 6.x (`Django>=5.2,<7`). Local runs use the installed Django. CI tests Django 5.2 on Python
3.11, 6.0 on 3.12 and 6.1 on 3.13 on Postgres, and 5.2 on 3.11 and 6.1 on 3.13 on SQLite.

`DECISIONS.md` records design decisions and their consequences; read it before changing behaviour.

## Licence

MIT.
