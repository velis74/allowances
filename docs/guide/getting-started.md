# Getting started

Nothing here needs a web framework. The engine is plain Python that does arithmetic against a
[store](/backends/) you hand it, so the walkthrough below runs in a script, a Celery worker, a
FastAPI handler or a Django view alike.

Two things have to be decided before the first call, and they are the same two everywhere:

- **A definition provider** — what features and currencies exist. Configuration, not rows.
- **A store** — where wallets and transactions live.

Every example on this page comes in two flavours. **Core** is the library on its own, with the
in-memory store; swap that store and the code is unchanged. **Django** is the same thing with both
choices already made for you — definitions from `settings.ALLOWANCES`, state in your database.

## Install

::: code-group

```bash [Core]
pip install allowances
```

```bash [Django]
pip install "allowances[django]"
```

:::

The core has no dependencies at all. The `django` extra adds Django and the ORM-backed store.

::: code-group

```python [Core]
# Nothing to set up. The in-memory store ships with the core:
from allowances.backends import MemoryStore

store = MemoryStore()
```

```python [Django]
# settings.py
INSTALLED_APPS = [
    # ...
    "allowances_django",
]

# then: python manage.py migrate
# Six tables appear, prefixed `allowances_`.
```

:::

`MemoryStore` is for tests and for trying things out — it dies with the process. For real storage,
either install the Django backend or [write your own](/backends/writing-a-backend): it is sixteen
methods.

## Declare what exists

Currencies are the units you count; features are the capabilities that consume them. The dict is
identical in both cases — only where it is read from differs.

::: code-group

```python [Core]
from allowances import DictProvider

provider = DictProvider({
    "currencies": {
        "email_send": {},
        "credits": {"general": True, "round_fn": "ceil"},
    },
    "features": {
        "send_email": {
            # A dedicated email budget, or half a credit if that runs out.
            "consumable": {"currencies": ["email_send", ("credits", 0.5)]},
            "subjects": {"user", "team"},
            "fulfillment_order": [
                ("consumable", "user"),  # spend the user's own budget first
                ("consumable", "team"),  # then fall back to the team's
            ],
        },
    },
})
```

```python [Django]
# settings.py
ALLOWANCES = {
    "currencies": {
        "email_send": {},
        "credits": {"general": True, "round_fn": "ceil"},
    },
    "features": {
        "send_email": {
            # A dedicated email budget, or half a credit if that runs out.
            "consumable": {"currencies": ["email_send", ("credits", 0.5)]},
            "subjects": {"user", "team"},
            "fulfillment_order": [
                ("consumable", "user"),  # spend the user's own budget first
                ("consumable", "team"),  # then fall back to the team's
            ],
        },
    },
}
```

:::

Read as: sending one email costs one `email_send`, or half a `credit`. Try the user's wallets before
the team's. `round_fn: "ceil"` on credits means a fractional cost is charged as a whole credit — you
never charge 0.5 of something you can't divide.

A provider is an interface, not a dict reader: definitions can just as well come from a database
table or a service call. See [`DefinitionProvider`](/api/core#definitionprovider).

## Name the subjects

A subject is whatever holds the licence — a user, a team, a device, a location. The core never sees
your objects, only a `(type, id)` pair.

::: code-group

```python [Core]
from allowances import SubjectRef

user = SubjectRef("user", 42)
team = SubjectRef("team", 7)
subjects = {"user": user, "team": team}
```

```python [Django]
# Pass the model instances you already have; the adapter makes the refs.
subjects = {"user": request.user, "team": team}
```

:::

The keys (`"user"`, `"team"`) are the names your `fulfillment_order` refers to. The `type` string is
yours to choose; the Django adapter uses `"app_label.modelname"`.

## Give somebody something to spend

Wallets hold balances. `plan_switch` is the usual way to open them — it closes whatever the subject
had and opens a new set atomically, which is exactly what putting someone on a plan means:

::: code-group

```python [Core]
from decimal import Decimal

from allowances import WalletSpec, WalletType, plan_switch

plan_switch(
    store,
    {"user": user},
    [
        WalletSpec(WalletType.CONSUMABLE, "email_send", Decimal("200")),
        WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("50")),
    ],
)
```

```python [Django]
from decimal import Decimal

from allowances import WalletSpec, WalletType
from allowances_django.shortcuts import plan_switch

plan_switch(
    {"user": request.user},
    [
        WalletSpec(WalletType.CONSUMABLE, "email_send", Decimal("200")),
        WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("50")),
    ],
)
```

:::

The only difference is the first argument: the core functions take the store explicitly, the Django
shortcuts supply it — and the provider — for you.

## Spend it

::: code-group

```python [Core]
from allowances import InsufficientBalanceError, fulfill

try:
    result = fulfill(store, "send_email", 1, subjects, provider)
except InsufficientBalanceError as exc:
    raise RuntimeError(f"Only {exc.result.granted} left")
```

```python [Django]
from allowances import InsufficientBalanceError
from allowances_django.shortcuts import fulfill

try:
    result = fulfill("send_email", 1, subjects)
except InsufficientBalanceError as exc:
    return http_402(f"Only {exc.result.granted} left")
```

:::

`fulfill` either covers the whole request or raises and rolls back — there is no half-charged state
to clean up. It returns a `FulfillmentResult`:

```python
result.requested   # Decimal("1")
result.granted     # Decimal("1")
result.satisfied   # True
result.guid        # UUID — the handle for attaching and reversing
result.transactions  # the ledger entries just written
```

## Close the loop

Fulfillment happens *before* the work: you take the quota, then send the email. Tell the ledger which
object it paid for once that succeeds:

::: code-group

```python [Core]
from allowances import ObjectRef, attach

message = send_the_email()
attach(store, result.guid, ObjectRef("message", message.id))
```

```python [Django]
from allowances_django.shortcuts import attach

message = send_the_email()
attach(result.guid, message)
```

:::

This matters more than it looks. A transaction with nothing attached is quota that was spent on
something that may never have happened — and [that is a query you can
run](/guide/reversals#finding-what-broke).

## Undo it

If the operation fails, give it back:

::: code-group

```python [Core]
from allowances import unfulfill

unfulfill(store, result.guid)
```

```python [Django]
from allowances_django.shortcuts import unfulfill

unfulfill(result.guid)
```

:::

Nothing is deleted. Each original entry gets a matching storno entry, so the history still shows what
was taken and what gave it back. Calling it twice is safe.

## Ask without spending

::: code-group

```python [Core]
from allowances import peek

if peek(store, "send_email", 10, {"user": user}, provider).satisfied:
    show_the_bulk_send_button()
```

```python [Django]
from allowances_django.shortcuts import peek

if peek("send_email", 10, {"user": request.user}).satisfied:
    show_the_bulk_send_button()
```

:::

`peek` runs the same planning walk as `fulfill` and writes nothing. It is a snapshot, not a
reservation — a concurrent caller can spend what it just told you was there.

## Where to go next

- [Concepts](/guide/concepts) — the vocabulary in full.
- [Fulfillment order](/guide/fulfillment-order) — the part worth understanding properly.
- [Sliding windows](/guide/sliding-windows) — "200 a day" without a reset job.
- [How the port works](/backends/) — if the store you want isn't one of the two shipped.
