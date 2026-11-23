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

await fulfill("send_email", 1, {"user": user, "team": team})
```

Synchronous code (a synchronous view, a Celery task, a management command) uses the same functions from
`allowances_django.sync.shortcuts`, without `await`:

```python
from allowances_django.sync.shortcuts import fulfill

fulfill("send_email", 1, {"user": request.user, "team": team})
```

Use a store directly for a second database or a custom provider:

```python
from allowances import fulfill
from allowances_django.store import DjangoStore
from allowances_django.subjects import subject_refs

await fulfill(DjangoStore(using="billing"), "send_email", 1, subject_refs({"user": user}), provider)
```

## Two stores

The package has two stores over the same models. Both run the same ORM queries
(`allowances_django.queries.DjangoQueries`); they differ in where those queries run.

| | `allowances_django.store.DjangoStore` | `allowances_django.sync.store.DjangoStore` |
| --- | --- | --- |
| For | the asynchronous API: `allowances`, `allowances_django.shortcuts` | the synchronous API: `allowances.sync`, `allowances_django.sync.shortcuts` |
| Queries run | on threads, never on the event loop | on the calling thread |
| `unit_of_work()` | `transaction.atomic()` on a thread of its own | `transaction.atomic()` on the calling thread |
| Needs | a running event loop | a thread without a running event loop |
| Default of | `ALLOWANCES_STORE` | `ALLOWANCES_SYNC_STORE` |

Both take `using=` (a database alias, default `None`: the default database) and have two methods besides the port,
used by the shortcuts: `resolve(ref)` (the model instance behind a reference, or `None`) and
`live_wallet_ids(subject=, wallet_type=, currency=, at=)`.

### The asynchronous store

Django has no asynchronous `transaction.atomic` or `select_for_update`, and a Django connection belongs to one thread.
`allowances_django.store.DjangoStore` therefore runs its queries on threads:

- **Outside a unit of work** a call runs as Django's own asynchronous ORM methods (`aget`, `afirst`, ...) do: through
  `sync_to_async` on Django's thread-sensitive executor, in autocommit.
- **Inside a unit of work** everything runs on one thread. The outermost `async with store.unit_of_work():` starts a
  thread of its own and enters `atomic()` on it. Every store call made inside the block runs on that thread: calls
  through any `DjangoStore` instance, nested blocks (savepoints) and the engine's own blocks. When the block ends,
  `atomic()` is exited on the thread, the thread's connections are closed and the thread ends.

Concurrent units of work in one process run on separate threads, with separate connections and transactions; they
never share a connection or an atomic block.

Tasks created inside a unit of work (`asyncio.gather`, `asyncio.create_task`) inherit it and share its transaction. A
nested block opened by such a task is a savepoint; nested blocks of different tasks run one after another, and while one
is open, store calls and blocks of other tasks wait until it ends. A failing nested block rolls back only its own
writes. A nested block that awaits a task whose store calls belong to the enclosing block never ends.

Each unit of work opens a database connection, so a connection pool (the `"pool"` option of Django's PostgreSQL backend
on psycopg 3) keeps it cheap.

Writes are as parallel as threads allow; the store does no asynchronous I/O. For asynchronous I/O on PostgreSQL use the
[adorm store](/backends/adorm).

A call made without a running event loop raises `StoreSuspendedError`: `allowances.sync` cannot drive this store.

### The synchronous store

`allowances_django.sync.store.DjangoStore` implements the port with coroutines whose bodies call the ORM directly and
never suspend, so `allowances.sync` runs the engine with it without an event loop (see
[Synchronous API](/guide/synchronous)). Its `unit_of_work()` enters and exits `transaction.atomic()` on the calling
thread, so a synchronous caller's own `with transaction.atomic():` block contains it.

Django refuses synchronous ORM calls on a thread that runs an event loop. Asynchronous code uses the asynchronous store,
or runs the synchronous API in a worker thread (`await asyncio.to_thread(...)`).

### Choosing the stores

```python
# settings.py (the defaults)
ALLOWANCES_STORE = "allowances_django.store.DjangoStore"
ALLOWANCES_SYNC_STORE = "allowances_django.sync.store.DjangoStore"
```

`ALLOWANCES_STORE` is the store of `allowances_django.shortcuts`, `allowances_django.permissions` and the asynchronous
checks of `AllowancesBackend`; `ALLOWANCES_SYNC_STORE` that of `allowances_django.sync.shortcuts`,
`allowances_django.sync.permissions` and the synchronous checks. Each is a dotted path to a callable or a callable,
called with no arguments for every call without a `store=` argument; `store=` wins. The synchronous store must never
suspend.

Besides the port, the shortcuts call two store methods: `groups_of` calls `resolve(ref)` and `revoke_permission` calls
`live_wallet_ids(...)`. A store without them serves every other shortcut.

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
from allowances_django.shortcuts import get_store
from allowances_django.subjects import resolve

ref = SubjectRef(wallet.subject_type, wallet.subject_id)
await get_store().resolve(ref)   # instance, or None if deleted
resolve(ref)                     # the same, in synchronous code
```

`resolve` returns `None` instead of raising, so an admin page can render an orphaned row.

## Concurrency

`unit_of_work()` is `transaction.atomic()` (on the unit of work's thread, for the asynchronous store) and nests; a
nested block that raises rolls back to its savepoint. `lock=True` and `lock_wallets` are `select_for_update()` where the
database has it; `lock_wallets` orders by `id`, so PostgreSQL takes the row locks in ascending id order. A locked
`get_record` is `FOR NO KEY UPDATE` on PostgreSQL: it serialises operations on one record, but does not block the
foreign-key check of a wallet's archival, which inserts archive rows referencing the record while holding that wallet's
lock.

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
await fulfill("send_email", 1, subjects, store=DjangoStore(using="billing"))
```

`allowances_django.sync.store.DjangoStore(using="billing")` does the same for the synchronous shortcuts.
