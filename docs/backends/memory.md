# In-memory backend

A complete store over plain dicts. It ships with the core and needs nothing installed.

```python
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
store.create_wallet(
    subject=user, wallet_type=WalletType.CONSUMABLE, currency="credits", balance=Decimal("10")
)

fulfill(store, "send_email", 3, {"user": user}, provider)
```

## What it's for

**Fast tests.** The engine's behaviour is arithmetic — proving it doesn't need a database. This library's own suite runs every engine test against this backend and again against Django, and the whole thing takes under a second.

**Keeping the port honest.** Every time the engine reaches for something only an ORM could do, this backend stops working. That is the alarm that the abstraction has leaked, and it goes off during development rather than during someone else's port.

**Trying things out.** Modelling a feature's currencies and fulfillment order is a lot easier when the feedback loop is a Python prompt.

## What it isn't

Not for production, and not close. State lives in a dict: it dies with the process and is invisible to every other one. Locking is a process-local `RLock`, which is exactly as much concurrency control as a single-process test needs and no more.

## How it behaves

**`unit_of_work()` is real.** The outermost block snapshots every collection and restores it if anything raises, so rollback genuinely rolls back. Nested blocks join the outer one, the way a nested `atomic()` does.

**Ordering is deterministic.** Rows carry an insertion sequence alongside their timestamp, so wallets created within the same microsecond still come back in a stable order — otherwise tests would pass or fail depending on clock resolution.

**Double-storno is refused.** The relational backends have a unique constraint; this one raises `ValueError`. A lost race there would silently refund twice, so it refuses just as loudly.

**Archiving works.** Archived wallets and transactions move to `store.archived_wallets` and `store.archived_transactions` rather than vanishing.

## Seeding modifiers

Modifiers are created by applications, not by the engine, so seeding them isn't part of the store port. This backend offers a helper anyway:

```python
store.add_modifier(subject=user, feature="send_email", factor=Decimal("0.5"))
```

The Django equivalent is creating a `LicenseModifier` row.
