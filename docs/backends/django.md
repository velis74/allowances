# Django backend

Relational store over the Django ORM, transactional, with row locks where the database supports them. Intended for
production.

```bash
pip install "allowances[django]"
```

```python
INSTALLED_APPS = ["allowances_django"]
```

## What you call

Application code normally uses [the shortcuts](/api/django#shortcuts), which supply the store and provider and accept
model instances as subjects:

```python
from allowances_django.shortcuts import fulfill

fulfill("send_email", 1, {"user": request.user, "team": team})
```

Use `DjangoStore` directly for a second database or a custom provider:

```python
from allowances.engine import fulfill
from allowances_django.store import DjangoStore
from allowances_django.subjects import subject_refs

fulfill(DjangoStore(using="billing"), "send_email", 1, subject_refs({"user": user}), provider)
```

## Tables

Eight, all prefixed `allowances_` (the app label is `allowances`, not `allowances_django`):

| Table | Holds |
| --- | --- |
| `allowances_wallet` | Live wallets |
| `allowances_transaction` | The ledger |
| `allowances_fulfillmentrecord` | One row per business operation |
| `allowances_licensemodifier` | Cost modifiers |
| `allowances_membership` | Direct memberships of subjects in groups |
| `allowances_walletarchive` | Spent-out and expired wallets |
| `allowances_transactionarchive` | Their entries |
| `allowances_allowanceslock` | Named rows locked to serialise membership changes |

## Why there is no ContentType

Subjects are stored as two plain strings, `subject_type` holding `"app_label.modelname"` and `subject_id` holding the
primary key as text, not as a `GenericForeignKey`. Consequences:

**The core does not depend on Django.** A `ContentType` foreign key would require it.

**Non-integer primary keys work.** Subjects keyed by UUID or slug are supported.

**No foreign key to `django_content_type`.** In a schema-per-tenant deployment, a foreign key from a per-tenant table
to the shared content-type table can be created, but it breaks `TRUNCATE` of the shared table once a second schema
exists, because PostgreSQL checks the constraint regardless of the connection's `search_path`.

Limitations: `select_related` cannot follow a subject, and the database does not detect a dangling reference. Resolve
the object when it is needed:

```python
from allowances.types import SubjectRef
from allowances_django.subjects import resolve

resolve(SubjectRef(wallet.subject_type, wallet.subject_id))  # instance, or None if deleted
```

`resolve` returns `None` instead of raising, so an admin page can render an orphaned row.

## Concurrency

`unit_of_work()` is `transaction.atomic()` and nests; a nested block that raises rolls back to its savepoint.
`lock=True` and `lock_wallets` are `select_for_update()` where the database has it; `lock_wallets` orders by `id`, so
PostgreSQL takes the row locks in ascending id order. A locked `get_record` is `FOR NO KEY UPDATE` on PostgreSQL: it
serialises operations on one record, but does not block the foreign-key check of a wallet's archival, which inserts
archive rows referencing the record while holding that wallet's lock.

`fulfill` locks every candidate wallet for the duration of the call, so two simultaneous calls against the same wallet
are serialised and cannot both spend the same balance. Every operation that locks wallets locks all of them in that one
ordered statement before reading balances, so two operations over the same wallets (a `fulfill` walking user then team
against one walking team then user, a transfer A→B against B→A) wait for each other instead of deadlocking.
`unfulfill` locks the fulfilment record before its wallets and reads the record's transactions without row locks:
locking only the transaction rows would let two concurrent reversals read the same not-yet-stornoed set, and locking
them before the wallets would invert the order a wallet's archival takes when it deletes them. The unique constraint
on `storno_of` additionally rejects a second storno of the same transaction.

`lock_memberships` locks the `AllowancesLock` row `"membership"` with `SELECT ... FOR UPDATE`, held until the
transaction ends, so two concurrent `add_member` calls cannot each pass the cycle check and together form a cycle.
Migration `0004_allowanceslock` creates the row. A missing row (an emptied table, a restored dump) is created in the
caller's transaction and locked. When two transactions create it at once, the second waits for the first; under
REPEATABLE READ or SERIALIZABLE the row the first created is not visible to the second, which then raises
`django.db.OperationalError`, and retrying the operation succeeds. On MySQL the same race can end in a deadlock error,
also resolved by a retry. On SQLite, which has no `FOR UPDATE`, the `IMMEDIATE` write lock serialises
them and no row is used.

## SQLite

Supported. Under concurrent `fulfill` calls against one wallet the balance is granted once, as on PostgreSQL
(`tests/django/test_concurrency.py` runs on both).

SQLite has no `SELECT ... FOR UPDATE`; its only lock is the database-wide write lock. `lock=True` compiles to a plain
select, and the write lock, held for the whole transaction, prevents a second unit of work from reading a balance this
one changes. This requires the transaction to take the write lock at `BEGIN`, not at its first write:

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

Without `transaction_mode` the ledger stays correct, because SQLite fails the second writer instead of committing it
over a stale read, but that writer gets `OperationalError: database is locked` during `fulfill`. With `IMMEDIATE` (or
`EXCLUSIVE`) it waits up to `timeout` instead. System check `allowances.W001` warns at startup when an SQLite database
uses neither mode.

SQLite limits decimal range. It stores `DecimalField` as a float, which Django rounds to the field's six decimal
places; this is exact to about fifteen significant digits. Amounts up to `999999999.999999` are read back exactly
(`tests/django/test_decimal_precision.py`). Larger amounts lose low digits (`1234567890.123456` is read back as
`1234567890.123460`) and raise `decimal.InvalidOperation` once the rounded value no longer fits the column. PostgreSQL
is exact for all twenty digits; use it for balances above 10^9.

::: tip Which to use
PostgreSQL for deployments with concurrent writers across processes, where one database-wide write lock limits
throughput. SQLite for single-process deployments, embedded and desktop installs, small services and test suites
without a database server.
:::

## Timestamps are settable

`created_at` on wallets and transactions uses `default=timezone.now`, not `auto_now_add`, so the store can write a row
at a given instant. `at=` therefore sets the stored timestamp, e.g. for testing a
[sliding window](/guide/sliding-windows) or backfilling history at its real timestamps.

## Admin

The seven ledger models are registered; `AllowancesLock` is not. Wallets, fulfilment records and modifiers are editable;
transactions, memberships and archives cannot be added or changed, only deleted. Memberships change through
`add_member` and `remove_member`, which reject cycles.

```python
ALLOWANCES_ADMIN_READ_ONLY = True
```

makes every allowances model view-only: no add, change or delete, for superusers too. Viewing still needs Django's
view or change permission. The setting is read on every request; the default is `False`.

After a wallet is opened, the engine changes its balance only through operations that write ledger entries
(`fulfill`, `unfulfill`, `credit`, `transfer`, `adjust_wallet`, a `plan_switch` adjustment). Editing a balance in the
admin changes it without a ledger entry; make corrections through those operations.

## Multiple databases

```python
DjangoStore(using="billing")
```

Every query the store issues is routed to that alias. Pass it through the shortcuts with `store=`:

```python
fulfill("send_email", 1, subjects, store=DjangoStore(using="billing"))
```
