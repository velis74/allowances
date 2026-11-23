---
description: 'Install allowances, declare features and currencies, pick the in-memory or Django store, then peek, fulfill, attach and unfulfill a first operation.'
---

# Getting started

The core needs no web framework. The engine is plain Python that works against the [store](/backends/) passed to
it, so the code below runs wherever an event loop runs: an asynchronous Django view, a FastAPI handler or
`asyncio.run` in a script.

Every function that reads or writes state is a coroutine, called with `await`. Synchronous code (a Celery task, a
synchronous Django view) imports the same names from `allowances.sync` and `allowances_django.sync.shortcuts`; see
[Synchronous API](/guide/synchronous).

Every call needs:

- **A definition provider**: which features and currencies exist. It is configuration.
- **A store**: where wallets and transactions are kept.

Each example has two variants. **Core** uses the library with `MemoryStore`; the code is the same with any other store.
**Django** reads definitions from `settings.ALLOWANCES` and keeps state in the database.

## Install

::: code-group

```bash [Core]
pip install allowances
```

```bash [Django]
pip install "allowances[django]"
```

:::

The core has no dependencies. The `django` extra adds Django and the ORM-backed stores.

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
# Eight tables appear, prefixed `allowances_`.
```

:::

`MemoryStore` keeps data only for the lifetime of the process and is meant for tests. For persistent storage, use the
Django backend or [write a backend](/backends/writing-a-backend) implementing the 22-method store port.

## Declare what exists

Currencies are the counted units; features are the capabilities that consume them. The dict is the same in both
variants; only its source differs.

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

Sending one email costs one `email_send`, or half a unit of `credits`. The user's wallets are tried before the
team's. `round_fn: "ceil"` on `credits` rounds a fractional cost up to a whole credit.

A provider is an interface: definitions can also come from a database table or a service call. See
[`DefinitionProvider`](/api/core#definitionprovider).

## Name the subjects

A subject is the holder of a licence: a user, a team, a device, a location. The core receives only a `(type, id)`
pair, not application objects.

::: code-group

```python [Core]
from allowances import SubjectRef

user = SubjectRef("user", 42)
team = SubjectRef("team", 7)
subjects = {"user": user, "team": team}
```

```python [Django]
# Pass the model instances you already have; the adapter makes the refs.
user = await request.auser()
subjects = {"user": user, "team": team}
```

:::

The keys (`"user"`, `"team"`) are the names `fulfillment_order` refers to. The `type` string is chosen by the
application; the Django adapter uses `"app_label.modelname"`.

## Open wallets

Wallets hold balances. `plan_switch` opens them for a plan: it closes the subject's plan wallets and opens a new set in
one transaction:

::: code-group

```python [Core]
from decimal import Decimal

from allowances import WalletSpec, WalletType, plan_switch

await plan_switch(
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

await plan_switch(
    {"user": user},
    [
        WalletSpec(WalletType.CONSUMABLE, "email_send", Decimal("200")),
        WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("50")),
    ],
)
```

:::

The core functions take the store as the first argument; the Django shortcuts supply the store and the provider.

Wallets outside a plan — a purchased credit pack, a spending budget — are opened with `open_wallet`
and funded with `credit`; see [Credits and budgets](/guide/credits-and-budgets).

## Consume

::: code-group

```python [Core]
from allowances import InsufficientBalanceError, fulfill

try:
    result = await fulfill(store, "send_email", 1, subjects, provider)
except InsufficientBalanceError as exc:
    raise RuntimeError(f"Only {exc.result.granted} left")
```

```python [Django]
from allowances import InsufficientBalanceError
from allowances_django.shortcuts import fulfill

try:
    result = await fulfill("send_email", 1, subjects)
except InsufficientBalanceError as exc:
    return http_402(f"Only {exc.result.granted} left")
```

:::

By default (`allow_partial=False`) `fulfill` covers the whole request or raises `InsufficientBalanceError` and
writes nothing. It returns a `FulfillmentResult`:

```python
result.requested   # Decimal("1")
result.granted     # Decimal("1")
result.satisfied   # True
result.guid        # UUID — the handle for attaching and reversing
result.transactions  # the ledger entries just written
```

## Attach the object

Fulfilment runs before the operation: the quota is consumed, then the email is sent. When the operation succeeds,
`attach` links the ledger entries to the object they paid for:

::: code-group

```python [Core]
from allowances import ObjectRef, attach

message = send_the_email()
await attach(store, result.guid, ObjectRef("message", message.id))
```

```python [Django]
from allowances_django.shortcuts import attach

message = send_the_email()
await attach(result.guid, message)
```

:::

A transaction without an attached object marks consumption whose operation may not have completed;
[Recovery sweep](/guide/reversals#recovery-sweep) lists such records.

## Reverse

If the operation fails, `unfulfill` reverses it:

::: code-group

```python [Core]
from allowances import unfulfill

await unfulfill(store, result.guid)
```

```python [Django]
from allowances_django.shortcuts import unfulfill

await unfulfill(result.guid)
```

:::

Nothing is deleted. Each original entry gets a matching storno entry, so the history shows both the consumption and
its reversal. A second call for the same `guid` writes nothing.

## Check without consuming

::: code-group

```python [Core]
from allowances import peek

if (await peek(store, "send_email", 10, {"user": user}, provider)).satisfied:
    show_the_bulk_send_button()
```

```python [Django]
from allowances_django.shortcuts import peek

if (await peek("send_email", 10, {"user": user})).satisfied:
    show_the_bulk_send_button()
```

:::

`peek` runs the same planning as `fulfill` and writes nothing. It is a snapshot, not a reservation: a concurrent
caller can consume the balance it reported.

## Where to go next

- [Concepts](/guide/concepts) — the vocabulary in full.
- [Fulfilment order](/guide/fulfillment-order) — the order wallets are drawn in.
- [Sliding windows](/guide/sliding-windows) — "200 a day" without a reset job.
- [How the port works](/backends/) — the store interface, for writing another backend.
