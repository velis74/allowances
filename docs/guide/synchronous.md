---
description: 'The synchronous API of allowances: allowances.sync, the synchronous Django shortcuts and permissions, the rule that the store never suspends, and use from Celery tasks and synchronous views.'
---

# Synchronous API

Every function of `allowances` that reads or writes state is a coroutine. Synchronous code — a Celery task, a
management command, a synchronous Django view, a script — calls the same functions from the synchronous modules:

| Asynchronous | Synchronous |
| --- | --- |
| `allowances` | `allowances.sync` |
| `allowances_django.shortcuts` | `allowances_django.sync.shortcuts` |
| `allowances_django.permissions` | `allowances_django.sync.permissions` |

Each synchronous function has the name, signature and docstring of its coroutine and returns what the coroutine returns.
Exceptions are the same objects and propagate unchanged.

## The core: `allowances.sync`

```python
from allowances.backends import MemoryStore
from allowances.sync import InsufficientBalanceError, SubjectRef, fulfill

store = MemoryStore()
try:
    result = fulfill(store, "send_email", 1, {"user": SubjectRef("user", 42)}, provider)
except InsufficientBalanceError as exc:
    ...
```

`allowances.sync` has every engine function of `allowances` as a plain function, and the same types, errors,
definitions and pure helpers (`window_start`), so one import line serves synchronous code.

`allowances.sync.unit_of_work(store)` opens a store's unit of work as a synchronous context manager; every call inside
the block runs in its transaction, and blocks nest with the store's savepoint semantics:

```python
from decimal import Decimal

from allowances.sync import WalletSpec, WalletType, credit, open_wallet, unit_of_work

with unit_of_work(store):
    pack = open_wallet(store, user, WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("0")), source="purchase")
    credit(store, pack.id, Decimal("500"))
```

## The store must never suspend

A synchronous function runs its coroutine to completion on the calling thread, without an event loop: it advances the
coroutine once and takes the result. The engine awaits nothing but store methods, so this works with a store whose
methods (and `unit_of_work`) await nothing that yields to an event loop. Such a store's methods are coroutines with
synchronous bodies.

| Store | Never suspends | `allowances.sync` |
| --- | --- | --- |
| `allowances.backends.MemoryStore` | yes | yes |
| `allowances_django.sync.store.DjangoStore` | yes | yes |
| `allowances_django.store.DjangoStore` | no (queries on threads) | no |
| `allowances_adorm.store.AdormStore` | no | no |

A call whose coroutine suspends is closed and raises `allowances.StoreSuspendedError` (an `AllowancesError` and a
`RuntimeError`): `allowances.sync` needs a store that never suspends; use the asynchronous API with the others.

`allowances.sync.driver.run(coroutine)` is the function that drives a coroutine this way, and
`allowances.sync.driver.synchronous(function, module)` makes a synchronous function of a coroutine function; both are
public for wrappers of an application's own coroutines over such a store. Pass `__name__` of the module that binds the
wrapper as `module`, so the wrapper is picklable.

## Django

`allowances_django.sync.shortcuts` has the shortcuts of `allowances_django.shortcuts`, and
`allowances_django.sync.permissions` has `grant_permission` and `revoke_permission` as synchronous functions (the other
names of `allowances_django.permissions` are re-exported: `AllowancesBackend`, `PermissionProvider`,
`effective_permissions`, `permission_feature`, `PERMISSION_PREFIX`, `PERMISSION_SOURCE`).

```python
from allowances import InsufficientBalanceError
from allowances_django.sync.shortcuts import attach, fulfill, unfulfill


def send(request, team):
    try:
        result = fulfill("send_email", 1, {"user": request.user, "team": team})
    except InsufficientBalanceError as exc:
        return http_402(f"Only {exc.result.granted} left")
    try:
        message = send_the_email()
    except Exception:
        unfulfill(result.guid)
        raise
    attach(result.guid, message)
```

Their default store is `settings.ALLOWANCES_SYNC_STORE`, by default `allowances_django.sync.store.DjangoStore`, which
runs the Django ORM on the calling thread. A `store=` argument must also be a store that never suspends. The store's
`unit_of_work()` is `transaction.atomic()`, so a caller's own `with transaction.atomic():` contains every synchronous
shortcut called inside it.

`AllowancesBackend.has_perm`, `get_all_permissions` and `has_module_perms` use this API with the store from the
backend's `get_sync_store()`.

### Celery tasks and management commands

Celery tasks and management commands are synchronous; they use the synchronous modules:

```python
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from allowances_django.sync.shortcuts import archive_expired_wallets, find_unattached_fulfillment_records, unfulfill


@shared_task
def allowances_housekeeping():
    archive_expired_wallets()
    for record in find_unattached_fulfillment_records(older_than=timezone.now() - timedelta(hours=1)):
        unfulfill(record.guid)
```

### Synchronous code called from asynchronous code

Django refuses synchronous ORM calls on a thread that runs an event loop (`SynchronousOnlyOperation`). Asynchronous
code uses the asynchronous API, or runs a synchronous function in a worker thread:

```python
import asyncio

from allowances_django.sync import shortcuts as sync_shortcuts

result = await asyncio.to_thread(sync_shortcuts.fulfill, "send_email", 1, subjects)
```
