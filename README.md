# allowances

Grants and consumable budgets — enforced before the fact, auditable after it. Storage-agnostic, with a Django backend.

📖 **Documentation** lives in `docs/`. Run `npm install && npm run docs:dev` to read it; it is not hosted anywhere yet.

---

## What it does

Applications keep needing to answer the same shape of question:

> Can this user send an email right now? How many API calls does this organisation have left this month? Does this device still have a service contract, and does the customer have enough coolant credit to fill it?

`allowances` answers it, records what the answer cost, and lets you take it back if the operation it paid for never happened.

It is **not a payment system** — no invoices, no card charges, no money anywhere. The units it counts are whatever you say they are. Use it for rights that are bought, expire, or get spent.

## Install

```bash
pip install "allowances[django]"   # or just `allowances` for the core
```

```python
INSTALLED_APPS = ["allowances_django"]
```

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

The engine sits between them and knows nothing about either's storage. It receives definitions through a provider and state through a sixteen-method store port, which is why the same code runs against Postgres or a dict.

```
   allowances.engine        peek · fulfill · attach · unfulfill · plan_switch
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
- **Multi-subject.** A licence can belong to a user, an organisation, a device, a location — resolved across all of them in one call.

## What it could also be

A permissions layer, if you push it. Not the point of the library, but the pieces line up: a grant with no expiry *is* a permission bit, the fulfillment order *is* a resolution order across user, team and organisation, and the ledger is the audit trail permission systems usually bolt on afterwards. You get things they rarely offer natively — rights that expire on a date, that may be exercised *n* times, or that are inherited from an organisation and revoked with it.

Only the plumbing is missing, and `NEXT_STEPS.md` already scopes it: a grant-only permissions backend, after which the decorators, mixins and checks you have already written keep working unchanged. See `docs/guide/index.md`.

## Development

```bash
pip install -e ".[test]"
cp tests/env.example.py tests/env.py   # local Postgres credentials
pytest                                 # 171 tests, both backends
ruff check . && ruff format --check .

npm install
npm run docs:dev
```

Postgres, not SQLite: the suite exercises `select_for_update` and partial unique constraints, and SQLite implements neither the way this relies on.

Locally that runs against whichever Django you have installed. CI runs the whole suite three times — Django 5.2 on Python 3.11, 6.0 on 3.12, 6.1 on 3.13 — which is what the `Django>=5.2,<7` pin is based on.

`DECISIONS.md` records every judgment call and what it would cost to revisit — read it before changing behaviour.

## Licence

MIT.
