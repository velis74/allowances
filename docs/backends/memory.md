# In-memory backend

A complete store over plain dicts. It is part of the core package and has no dependencies.

```python
from decimal import Decimal

from allowances import DictProvider, SubjectRef, WalletType, fulfill
from allowances.backends import MemoryStore

store = MemoryStore()
provider = DictProvider({
    "currencies": {"credits": {}},
    "features": {
        "send_email": {
            "consumable": {"currencies": ["credits"]},
            "fulfillment_order": [("consumable", "user")],
        },
    },
})

user = SubjectRef("app.user", 1)
await store.create_wallet(
    subject=user, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
)

await fulfill(store, "send_email", 3, {"user": user}, provider)
```

Every method is a coroutine with a synchronous body that never suspends, so the store serves both the asynchronous API
and [`allowances.sync`](/guide/synchronous): with `from allowances.sync import fulfill` the last line is
`fulfill(store, "send_email", 3, {"user": user}, provider)`.

## Uses

**Tests.** Engine tests run without a database; the library's engine tests run against this backend, both
Django stores and the adorm store.

**Port check.** Engine code that requires ORM features fails on this backend.

**Experiments.** Feature definitions (currencies, fulfilment order) can be tried in a Python shell.

## Limitations

Not for production. State is held in process memory, is lost when the process ends and is not shared with other
processes. Locking is a process-local `RLock`.

An engine call on an event loop runs without interleaving with other tasks, as the store never suspends. The `RLock` is
re-entrant per thread: a caller's own `async with store.unit_of_work():` block that awaits something suspending is not
isolated from other tasks on the same thread, and its rollback also discards their writes made during the block.

## Behaviour

**`unit_of_work()` rolls back.** Every block snapshots every collection and restores it when the block raises. A
nested block that raises discards only its own writes, as a nested `atomic()` rolls back to its savepoint.

**Ordering is deterministic.** Rows carry an insertion sequence alongside their timestamp, so wallets created within
the same microsecond come back in a stable order regardless of clock resolution.

**Double-storno is refused.** The relational backends have a unique constraint; this one raises `ValueError`.

**Locks are the store-wide lock.** `lock_wallets` returns the wallets in ascending id order without taking anything
further, and `lock_memberships` does nothing: `unit_of_work` already holds the store's `RLock`. Memberships are rolled
back with everything else.

**Archiving moves rows.** Archived wallets and transactions move to `store.archived_wallets` and
`store.archived_transactions`.

## Seeding modifiers

Modifiers are created by applications, not by the engine, so seeding them is not part of the store port. This backend
has a helper for it:

```python
await store.add_modifier(subject=user, feature="send_email", factor=Decimal("0.5"))
```

The Django equivalent is creating a `LicenseModifier` row.
