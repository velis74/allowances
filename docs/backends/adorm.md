---
description: 'The adorm store: the allowances tables through adorm, with asynchronous I/O on PostgreSQL (asyncpg) or the Django ORM on adorm threads. Setup, profiles, sessions and generated models.'
---

# adorm backend

`allowances_adorm.store.AdormStore` implements the store port with [adorm](https://docs.velis.si/adorm/), an
asynchronous ORM with the Django ORM's API. It reads and writes the tables of the [Django backend](/backends/django)
and runs on either of adorm's drivers:

- **the native PostgreSQL driver** (asyncpg): queries run on the event loop, no threads, no Django code;
- **the Django driver**: the Django ORM on a thread owned by one pool slot, on every database Django supports.

Every method suspends, so the store serves only the asynchronous API (`allowances`, `allowances_django.shortcuts`), not
[`allowances.sync`](/guide/synchronous).

## Install

```bash
pip install "allowances[adorm]"                    # adorm with its Django driver
pip install "allowances[adorm]" "adorm[asyncpg]"   # ...and the native PostgreSQL driver
```

The native driver needs Django's PostgreSQL backend on psycopg 3 (`psycopg`, not `psycopg2`).

## Setup

The tables and migrations stay those of `allowances_django`, which stays in `INSTALLED_APPS`. adorm is configured by
the application, from Django settings:

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "allowances_django",
    "adorm.contrib.django",   # configures adorm from ADORM on django.setup()
]

ADORM = {
    "profiles": {
        "default": {"driver": "adorm.drivers.django.DjangoDriver", "alias": "default"},
        "asyncpg": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "alias": "default"},
    },
    "pool": {"max_connections": 10},
}
```

```bash
python manage.py migrate allowances
```

`ADORM` holds the keyword arguments of `adorm.configure()`; without `"adorm.contrib.django"`, call
`adorm.configure(...)` after `django.setup()`. A profile is a connection profile: a driver and the Django database
alias it connects to. Both profiles above use the `default` database. `max_connections` bounds the connections of each
process; processes × pool must stay under PostgreSQL's `max_connections`. See adorm's
[connection profiles](https://docs.velis.si/adorm/topics/profiles) and [pool](https://docs.velis.si/adorm/topics/pool).

## Using the store

```python
from allowances import fulfill
from allowances_adorm.store import AdormStore

store = AdormStore("asyncpg")   # every call on the adorm session ("asyncpg", "default")
result = await fulfill(store, "send_email", 1, subjects, provider)
```

`AdormStore(using=None, *, session_name="default")`:

- With `using` (a profile name), every call runs on the adorm session `(using, session_name)`. `session_name`
  separates independent flows on one profile.
- Without `using`, every call runs on adorm's current session (`adorm.current_session()`), so the store's work joins an
  `adorm.atomic()` block the caller has open on that session:

```python
import adorm

store = AdormStore()

async with adorm.atomic():
    await fulfill(store, "send_email", 1, subjects, provider)
    await Message.objects.create(...)       # an adorm model of the application, same transaction
```

The store holds no connection. It caches fulfilment record guid → primary key.

With the Django shortcuts, pass it as `store=` or name it in `ALLOWANCES_STORE` (see
[Choosing the stores](/backends/django#choosing-the-stores)). It also has the two methods the shortcuts `groups_of`
and `revoke_permission` call: `resolve(ref)` (through the Django ORM, since the instances are the application's Django
models) and `live_wallet_ids(...)`.

## Execution and locking

- `unit_of_work()` is `session.atomic()` on the store's session; a nested block is a savepoint.
- `lock_wallets` is `filter(id__in=...).order_by("id").select_for_update()`: PostgreSQL takes the row locks in
  ascending id order. Ids are compared as integers (`"5"` and `5` are one id); ids that cannot be one are left out.
- `lock=True` on `get_wallet`, `find_wallets`, `find_scheduled_wallets`, `find_expired_wallets` and
  `transactions_for_record` is `select_for_update()`.
- A locked `get_record` is `FOR NO KEY UPDATE` where the profile's driver supports it, plain `FOR UPDATE` otherwise; see
  [Django backend concurrency](/backends/django#concurrency) for why.
- `lock_memberships` locks the `AllowancesLock` row `"membership"` with `SELECT ... FOR UPDATE`. A missing row is
  created in a savepoint; when a concurrent unit of work created it and the isolation level hides it (REPEATABLE READ,
  SERIALIZABLE), `adorm.OperationalError` is raised, and a retry succeeds.
- On SQLite (Django driver) `select_for_update` compiles to a plain select and the transaction's write lock serialises
  writers; set `transaction_mode` to `IMMEDIATE` as for the [Django backend](/backends/django#sqlite).

`created_at` and `updated_at` are written by the store (UTC now, or `at=`), so no Django default or `auto_now` is
needed.

On the native driver no Django Python code runs. A write to a model with live Django signal receivers raises adorm's
`NotSupportedError` unless the profile lists those receivers in `covered_receivers`; `allowances_django` registers none.

## Generated models

The store queries adorm models generated from the `allowances_django` models, committed in
`allowances_adorm/models/`. They are part of the package: an application does not generate them. They are
regenerated after every change of the Django models with

```bash
adorm generate --settings tests.settings --output allowances_adorm/models --line-length 120 allowances
```

and CI checks that they match the Django models (`adorm generate --check`).
