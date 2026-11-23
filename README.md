# allowances

Grants and consumable budgets — enforced before the fact, auditable after it. Storage-agnostic, with a Django backend that also serves Django permissions: expiring, inherited through nested groups, held by any model, and each use can be recorded.

📖 **Documentation:** [docs.velis.si/allowances](https://docs.velis.si/allowances). The source lives in `docs/` — `npm install && npm run docs:dev` serves it locally.

---

## What it does

Applications keep needing to answer the same shape of question:

> Can this user send an email right now? How many API calls does this organisation have left this month? Does this device still have a service contract, and does the customer have enough coolant credit to fill it?

`allowances` answers it, records what the answer cost, and lets you take it back if the operation it paid for never happened.

The same wallets hold rights that are not spent. In Django they answer `user.has_perm`, `@permission_required` and `{% if perms.… %}` through an authentication backend — see the [Permissions quick start](https://docs.velis.si/allowances/guide/permissions-quick-start).

It is **not a payment system** — no invoices, no card charges, no money anywhere. The units it counts are whatever you say they are. Use it for rights that are bought, expire, or get spent.

## Install

Django is optional. The core is `import allowances` and depends on nothing at all — not Django, not a database driver, not anything outside the standard library. Everything that matters (the ordering walk, conversion, rounding, overdraft, sliding windows) lives there, and talks to storage through a twenty-two-method port.

```bash
pip install allowances            # the engine, zero dependencies
pip install "allowances[django]"  # ...and the Django models, store and admin
```

The Django binding is one extra line, and the rest of this README assumes you took it:

```python
INSTALLED_APPS = ["allowances_django"]
```

Without it you supply your own `Store` — a dict-backed one ships in the box for tests, and [writing another](https://docs.velis.si/allowances/backends/writing-a-backend) is an afternoon.

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
attach(result.guid, message)   # record what the quota bought
# ...or unfulfill(result.guid) if it failed
```

## The shape of it

Two layers, kept apart. **Definition** — what rights exist at all: features, currencies, conversion rates, priority order. Configuration, not rows. **State** — what a subject holds and has spent: wallets and a transaction ledger.

The engine sits between them and knows nothing about either's storage. It receives definitions through a provider and state through a twenty-two-method store port, which is why the same code runs against Postgres, SQLite or a dict.

```
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

- **Grants and consumables in one flow.** A boolean entitlement and a metered budget resolve together, in an order you declare.
- **Multi-source payment.** One operation can draw from a dedicated budget, then general credits, then a promotional grant, at conversion rates you set.
- **Nothing edited in place.** Consumption writes a ledger entry; reversal writes a matching storno; spent-out wallets move to an archive.
- **Sliding windows without a reset job.** "200 a day" works by ageing consumption out of a window, not by a nightly job that can fail.
- **Multi-subject.** A licence can belong to a user, an organisation, a device, a location — resolved across all of them in one call, together with the groups each one is a member of.

## As a permissions layer

`allowances_django.permissions.AllowancesBackend` answers Django's permission checks from grant wallets, so everything that calls `has_perm` — views, templates, the admin, DRF — reads them. It does what `django.contrib.auth` permissions do not:

- **Rights that end.** A grant has `valid_from` and `valid_until` and stops counting on its own, without a cleanup job.
- **Groups of groups, and groups that are not `auth.Group`.** Any model can be a group — a department, an organisation, a club — nested to any depth; a membership that would form a cycle is rejected.
- **Rights held by anything.** A device, an organisation or a club membership holds and inherits rights as a user does.
- **Rights that are bought, come with a plan or are used up.** A right is a wallet, so `plan_switch` and purchases grant it like a quota, and a right can allow *n* uses.
- **A record of use and of origin.** `fulfill` writes each use to the ledger with its actor; `effective_allowances` lists every right and balance with the subject or group it comes from.

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

`effective_allowances` returns everything the user holds, budgets and rights alike, in the library's terms; `effective_permissions` keeps the Django permissions. The backend works alone or chained after `ModelBackend`. It has no object-level permissions, no deny rules and no admin UI for granting.

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

No database server required: the suite defaults to a file-backed SQLite. Copy `tests/env.example.py` to `tests/env.py` to point it at a local Postgres instead, and `ALLOWANCES_TEST_DB=sqlite uv run pytest` to go back. Both engines are supported and both are covered — including `tests/django/test_concurrency.py`, which races eight threads for the same wallet and insists it is granted exactly once.

Locally that runs against whichever Django you have installed. CI runs the suite five times — Django 5.2 on Python 3.11, 6.0 on 3.12 and 6.1 on 3.13 against Postgres, plus each end of that range against SQLite — which is what the `Django>=5.2,<7` pin is based on.

`DECISIONS.md` records every judgment call and what it would cost to revisit — read it before changing behaviour.

## Licence

MIT.
