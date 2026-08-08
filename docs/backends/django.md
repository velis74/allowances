# Django backend

The production backend. Relational, properly transactional, and it locks.

```bash
pip install "allowances[django]"
```

```python
INSTALLED_APPS = ["allowances_django"]
```

## What you call

Application code normally uses [the shortcuts](/api/django#shortcuts), which supply the store and provider for you and accept model instances as subjects:

```python
from allowances_django.shortcuts import fulfill

fulfill("send_email", 1, {"user": request.user, "team": team})
```

Drop to `DjangoStore` directly only when you need a second database or a custom provider:

```python
from allowances.engine import fulfill
from allowances_django.store import DjangoStore
from allowances_django.subjects import subject_refs

fulfill(DjangoStore(using="billing"), "send_email", 1, subject_refs({"user": user}), provider)
```

## Tables

Six, all prefixed `allowances_` (the app label is `allowances`, not `allowances_django`):

| Table | Holds |
| --- | --- |
| `allowances_wallet` | Live wallets |
| `allowances_transaction` | The ledger |
| `allowances_fulfillmentrecord` | One row per business operation |
| `allowances_licensemodifier` | Cost modifiers |
| `allowances_walletarchive` | Spent-out and expired wallets |
| `allowances_transactionarchive` | Their entries |

## Why there is no ContentType

Subjects are stored as two plain strings — `subject_type` holding `"app_label.modelname"`, `subject_id` holding the primary key as text — rather than as a `GenericForeignKey`.

This buys three things:

**The core stays free of Django.** A `ContentType` foreign key would drag the ORM into the one place that must not have it.

**Non-integer primary keys work.** The old schema used a `PositiveBigIntegerField` for the object id, which quietly ruled out subjects keyed by UUID or slug.

**Schema-per-tenant deployments stop fighting `django_content_type`.** A real foreign key from a per-tenant table to the shared content-type table is legal to create — Postgres doesn't care which schema either side lives in — but it breaks `TRUNCATE` of that shared table the moment a second schema exists, because Postgres considers the constraint regardless of the connection's `search_path`. The previous schema carried `db_constraint=False` on every such column to work around it. Now there is nothing to work around.

The cost: `select_related` can't follow a subject, and a dangling reference isn't caught by the database. Resolve when you need the object:

```python
from allowances.types import SubjectRef
from allowances_django.subjects import resolve

resolve(SubjectRef(wallet.subject_type, wallet.subject_id))  # instance, or None if deleted
```

`None` rather than an exception, so an admin page can render an orphaned row instead of falling over on it.

## Concurrency

`unit_of_work()` is `transaction.atomic()`. `lock=True` is `select_for_update()` where the database has it.

`fulfill` locks every candidate wallet for the duration, so two simultaneous calls against the same wallet serialise rather than both spending the same balance. `unfulfill` locks the fulfillment record itself — locking only the rows would let two concurrent reversals each read the same not-yet-stornoed set. The unique constraint on `storno_of` is the backstop if that ever fails.

## SQLite

Supported, and tested the same way Postgres is — the concurrency suite in `tests/django/test_concurrency.py` runs on both and passes on both. Eight threads racing for the same wallet, released from a barrier together, get the balance granted exactly once.

It reaches that result by a different route. SQLite has no `SELECT ... FOR UPDATE` and never will, because it has nothing finer to offer than the single write lock it holds over the whole database. `lock=True` therefore compiles to a plain select there, and the guarantee the engine actually needs — that no second unit of work reads a balance this one is about to change — comes from the write lock covering the entire transaction instead. Coarser than row locking, and strictly stronger.

That only holds if the transaction takes the write lock when it opens rather than when it first writes, which is one line of configuration:

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",          # take the write lock at BEGIN
            "timeout": 20,                            # ...and wait that long for it
            "init_command": "PRAGMA journal_mode=WAL;",  # readers don't block the writer
        },
    }
}
```

Leave `transaction_mode` out and the ledger is still correct — SQLite fails the second writer rather than committing it over a stale read — but the failure arrives as `OperationalError: database is locked` in the middle of a `fulfill`. `IMMEDIATE` turns that error into a wait. The app registers a system check, `allowances.W001`, that says so at startup rather than under load.

What SQLite genuinely gives up is decimal range, not correctness. It stores `DecimalField` as a float and Django rounds back to the field's six decimal places, which is exact to about fifteen significant digits: every amount up to `999999999.999999` survives the round trip untouched, and `tests/django/test_decimal_precision.py` holds both engines to that. Past it the low digits go — quietly at first (`1234567890.123456` comes back as `1234567890.123460`), then as `decimal.InvalidOperation` once the rounded value no longer fits the column. Postgres is exact across all twenty digits. Balances above a billion are a real reason to choose it; the ones this page used to give were not.

::: tip Which to use
Postgres for a deployment with concurrent writers across processes, where one global write lock becomes the bottleneck. SQLite for single-process deployments, embedded and desktop installs, small services, and every test suite that would rather not run a database server.
:::

## Timestamps are settable

`created_at` on wallets and transactions uses `default=timezone.now` rather than `auto_now_add`, so the store can place a row at a specific instant. That is what makes `at=` work end to end — testing a [sliding window](/guide/sliding-windows) without waiting a day, or backfilling history at its real timestamps.

## Admin

All six models are registered. Wallets and modifiers are editable; transactions and archives are read-only, deliberately.

Balances are meant to move through `fulfill` and `unfulfill`, each of which writes an entry explaining why. Editing a balance in the admin leaves a number that changed for no recorded reason — precisely the thing the ledger exists to prevent. If you need a manual correction, write a transaction for it.

## Multiple databases

```python
DjangoStore(using="billing")
```

Every query the store issues is routed to that alias. Pass it through the shortcuts with `store=`:

```python
fulfill("send_email", 1, subjects, store=DjangoStore(using="billing"))
```
