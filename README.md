# allowances

Grants and consumable budgets — enforced before the fact, auditable after it. Storage-agnostic, with a Django backend.

📖 **Documentation:** [docs.velis.si/allowances](https://docs.velis.si/allowances). The source lives in `docs/` — `npm install && npm run docs:dev` serves it locally.

---

## What it does

Applications keep needing to answer the same shape of question:

> Can this user send an email right now? How many API calls does this organisation have left this month? Does this device still have a service contract, and does the customer have enough coolant credit to fill it?

`allowances` answers it, records what the answer cost, and lets you take it back if the operation it paid for never happened.

It is **not a payment system** — no invoices, no card charges, no money anywhere. The units it counts are whatever you say they are. Use it for rights that are bought, expire, or get spent.

## Install

Django is optional. The core is `import allowances` and depends on nothing at all — not Django, not a database driver, not anything outside the standard library. Everything that matters (the ordering walk, conversion, rounding, overdraft, sliding windows) lives there, and talks to storage through a sixteen-method port.

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

The engine sits between them and knows nothing about either's storage. It receives definitions through a provider and state through a sixteen-method store port, which is why the same code runs against Postgres, SQLite or a dict.

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

Only the plumbing is missing, and `NEXT_STEPS.md` already scopes it: a grant-only permissions backend, after which the decorators, mixins and checks you have already written keep working unchanged. See [the guide](https://docs.velis.si/allowances/guide/).

## Development

```bash
pip install -e ".[test]"
pytest                                 # 188 tests, both stores, no setup
ruff check . && ruff format --check .

npm install
npm run docs:dev
```

No database server required: the suite defaults to a file-backed SQLite. Copy `tests/env.example.py` to `tests/env.py` to point it at a local Postgres instead, and `ALLOWANCES_TEST_DB=sqlite pytest` to go back. Both engines are supported and both are covered — including `tests/django/test_concurrency.py`, which races eight threads for the same wallet and insists it is granted exactly once.

Locally that runs against whichever Django you have installed. CI runs the suite five times — Django 5.2 on Python 3.11, 6.0 on 3.12 and 6.1 on 3.13 against Postgres, plus each end of that range against SQLite — which is what the `Django>=5.2,<7` pin is based on.

`DECISIONS.md` records every judgment call and what it would cost to revisit — read it before changing behaviour.

## Licence

MIT.
