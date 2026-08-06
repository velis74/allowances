# django-licensing

A flexible, extensible licensing and entitlement management library for Django applications.

---

## Mission Statement

Most Django applications eventually need to answer questions like:
*"Can this user send an email right now? How many API calls does this organisation have left? Does this object have an active subscription?"*

`django-licensing` provides a principled, audit-ready answer to these questions — without coupling your business logic to payment providers, feature flags, or ad-hoc permission checks scattered across the codebase.

The library is inspired by concepts from Google Play Billing (entitlements, consumables, grants) but is designed for server-side enforcement in multi-tenant Django applications, with a clean separation between *what rights exist* (definition) and *what rights a subject currently holds* (state).

---

## Problems It Solves

- **Fragmented permission checks** — license enforcement is a first-class concern, not an afterthought bolted onto Django permissions or a SaaS billing webhook.
- **Mixed right types** — some features require a boolean grant ("has a Pro subscription"), others require a consumable budget ("200 emails per day"), and both need to coexist in a single fulfillment flow.
- **Multi-currency spending** — a single operation may be payable from multiple sources (specific budget, general credits, promotional grants), with a deterministic priority order.
- **Audit and traceability** — every grant and every unit of consumption is recorded; you always know what is consuming your users' licences and can implement recovery flows when something goes wrong.
- **Multi-subject licensing** — a licence can be held by a user, an organisation, or any other domain object; the fulfillment engine resolves across all of them in a defined priority order.

---

## Core Concepts

### Subject

Any Django model instance that can hold a licence: a `User`, an `Organisation`, a `Device`, or any other domain object. Subjects are referenced via `GenericForeignKey`.

Each feature declares which subject types it recognises. At fulfillment time, the caller provides a `dict[subject_name, subject_instance]` mapping, e.g.:

```python
subjects = {"device": my_device, "team": my_team, "location": my_location}
```

### Currency

A named unit of entitlement, defined in settings. Currencies are either *specific* (e.g. `email_send`, `coolant_liters`) or *general-purpose* (e.g. `credits`). General-purpose currencies act as a fallback for any feature that accepts them. Balance and all amounts are `Decimal` to support both integer and fractional consumables.

### Feature

A named capability defined in settings, such as `send_email` or `coolant_fill`. A feature may have a `grant` section, a `consumable` section, or both. Each section declares which currencies it accepts and at what conversion rate. Example:

```python
LICENSING = {
    "features": {
        "send_email": {
            "grant": {
                "currencies": ["email_send"],
            },
            "consumable": {
                "currencies": ["email_send", ("credits", 0.5)],
            },
            "subjects": {"device", "team", "location"},
            "fulfillment_order": [("grant", "device"), ("grant", None), "location", "team"],
            "overdraft": False,
        },
        "coolant_fill": {
            "consumable": {
                "currencies": ["coolant_liters", ("credits", 3.27)],
            },
            "subjects": {"device", "team"},
            "fulfillment_order": [("grant", None), "device", "team"],
            "overdraft": False,
        },
    },
    "currencies": {
        "email_send": {"general": False, "round_fn": "floor"},
        "credits": {"general": True, "round_fn": None},
        "coolant_liters": {"general": False, "round_fn": None},
    },
}
```

A currency tuple `("credits", 0.5)` means: consuming 1 unit of this feature costs 0.5 credits.

### Fulfillment Order

Each feature declares a `fulfillment_order` list of `(type, subject)` tuples, where either element may be `None` as a wildcard:

- `("grant", "device")` — grant wallet held by the `device` subject
- `("grant", None)` — grant wallet held by any subject
- `("consumable", "location")` — consumable wallet held by the `location` subject
- `"location"` — shorthand for `(None, "location")`

The engine walks this list in order, drawing from each matching wallet until the requested quantity is satisfied. After a full pass, if quantity is still not satisfied, a second pass is made over wallets with `overdraft: true`.

Alternatively, `fulfillment_order` may be a dotted string reference to a callable that receives a QuerySet of candidate wallets and returns it ordered:

```python
"fulfillment_order": "myapp.licensing.my_ordering_fn"
```

### Wallet

A subject's holding of a particular currency. A wallet has:

- `balance` (Decimal)
- `valid_from`, `valid_until` (optional)
- `overdraft_limit` (optional Decimal)
- `first_overdraft_at` (timestamp, set automatically when balance first goes negative — used to enforce `max_overdraft_duration`)
- `reset_config` (optional JSON) — for sliding-window or calendar-based replenishment

The effective balance of a sliding-window wallet is computed on-the-fly from its transactions within the current window. Fully consumed wallets are archived rather than deleted.

**Reset config options:**

| Setting | Meaning |
|---|---|
| `None` | Rolling window (last N seconds/hours/days) |
| `time` | Daily reset at given time |
| `day_of_week + time` | Weekly reset |
| `day_of_month + time` | Monthly reset |

All timestamps are stored in UTC.

### Transaction

Every change to a wallet balance is recorded as a transaction (grant, consumption, or reversal). Transactions are the authoritative ledger.

Each transaction may carry a reference to the domain object that caused it (`object_ct` + `object_id`). This reference is written in two stages: the transaction is created at fulfillment time, and the domain object is attached once the business operation completes.

Reversals are recorded as explicit storno transactions with a `storno_of` FK to the original, enabling full audit trails and preventing double-storno.

### FulfillmentRecord

Groups all transactions belonging to a single logical business operation, identified by a UUID (`guid`). The guid:

- Is generated automatically if not provided
- Can be supplied by the caller to group multiple `fulfill` calls (e.g. "200 emails now, 50 tomorrow")
- Is stored on the domain model to enable correlation between business records and licence transactions
- Is used as the key for `attach` and `unfulfill` operations

A background job may monitor `FulfillmentRecord`s with unattached transactions to trigger recovery flows (storno or retry) for incomplete business operations.

### Modifier

A time-limited, subject-scoped factor that adjusts the cost of consuming a specific feature. A modifier of `0.5` on `send_email` means each send costs half a unit. Where multiple modifiers apply, the most favourable (`max`) is used. Rounding (floor/ceil/round/None) is defined per currency.

---

## Core API

```python
# Check availability without consuming
peek(feature, quantity, subjects) → FulfillmentResult

# Consume licence
fulfill(feature, quantity, subjects, guid=None, allow_partial=False) → FulfillmentResult

# Attach domain object to transactions (FIFO by timestamp)
attach(guid, obj) → None

# Reverse a fulfillment
unfulfill(guid) → None

# Atomically close all wallets for the given subjects and open new ones
plan_switch(subjects, open_wallets: list[WalletSpec], effective_date=None) → None
```

`plan_switch` sets `valid_until = effective_date` on all currently active wallets for the given subjects and creates new wallets from `open_wallets` with `valid_from = effective_date`. If `effective_date` is `None`, the switch is immediate. The entire operation is atomic. What to carry over between old and new wallets (if anything) is the responsibility of the application — use `peek` to inspect current state before calling `plan_switch`.

### FulfillmentResult

```python
FulfillmentResult:
    requested: Decimal
    granted: Decimal
    satisfied: bool          # granted == requested
    transactions: list[Transaction]
    guid: UUID
    modifier_applied: Decimal | None
```

---

## Definition vs. State

| Concern | What it answers | Where it lives |
|---|---|---|
| **Definition** | What features exist, what currencies they accept, what the rules are | `settings.py` (currently) |
| **State** | What a specific subject currently holds and has consumed | PostgreSQL via Django ORM |

The definition layer is accessed through a **provider interface**. The current implementation reads from `settings.py`. A future `ServerProvider` will fetch definitions from a remote licence server — no changes to the state layer or fulfillment engine will be needed.

---

## DB Models

- `Wallet` — subject's holding of a currency
- `WalletArchive` — fully consumed wallets
- `Transaction` — ledger entry (grant, consumption, storno)
- `TransactionArchive` — archived transactions
- `FulfillmentRecord` — groups transactions by guid
- `LicenseModifier` — time-limited cost modifier per subject and feature

---

## Out of Scope

- **Plans and plan management** — what constitutes a "free" or "professional" plan, when to switch, and whether to carry over balance are entirely the application's responsibility. The library provides `plan_switch` to ensure the wallet transition is atomic and race-condition-free.
- **Automatic plan expiry** — scheduled downgrades (e.g. professional → free at end of billing period) are handled by the application setting appropriate `valid_from`/`valid_until` values via `plan_switch`. No background job is required; the fulfillment engine respects these dates automatically.
- **Licence server** — a remote `DefinitionProvider` is a natural next step but is explicitly out of scope for this library. The provider interface is designed to make this a drop-in replacement.
- **Payment integration** — this library enforces licences; it does not issue them. Granting wallets in response to payment events is the responsibility of the application.
- **Reconciliation** — when a subject's entitlement decreases (e.g. downgrade), the application is responsible for detecting which domain objects exceed the new limit and calling `unfulfill` for them. The library provides `peek` to support this flow.

---

## Project Status

This is a fresh, standalone extraction of the design above into its own repository/package,
built incrementally by "concern":

- **Concern 1 (done)** — DB schema only: `Wallet`, `WalletArchive`, `Transaction`,
  `TransactionArchive`, `FulfillmentRecord`, `LicenseModifier`. No `peek`/`fulfill`/`attach`/
  `unfulfill`/`plan_switch` engine, no settings-`LICENSING` provider, no fulfillment-order walk,
  no sliding-window balance computation. See `NEXT_STEPS.md` for what's next and `DECISIONS.md`
  for schema-level decisions the README above leaves open.
- Consumers of this library (e.g. KlubIS, see its `CLAUDE.md` poglavje 3/4 for the
  "reusable module + setup module" pattern this library is designed to fit into) should treat
  everything beyond the DB schema as **not yet implemented**.

---

## Installation (development)

```bash
mkvirtualenv django-licensing   # or: python3.11 -m venv ~/.venv/django-licensing
pip install -e ".[test]"
```

The test suite needs a local Postgres database (see "Definition vs. State" above — `Decimal` and
`JSONField` usage is exercised against Postgres, not SQLite, to match the intended production
engine). Copy `tests/env.example.py` to `tests/env.py` (gitignored) and fill in your local
credentials, then create the two databases it expects (`django_licensing`,
`test_django_licensing`, or let Django create/drop the test one automatically if your DB user has
`CREATEDB`).

```bash
pytest
ruff check .
ruff format --check .
```
