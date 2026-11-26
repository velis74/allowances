# Writing a backend

A store implements twenty-two methods. The [Django](/backends/django), [adorm](/backends/adorm) and
[in-memory](/backends/memory) backends are complete implementations; `allowances/backends/memory.py` is the shortest.

## Methods

```python
from contextlib import AbstractAsyncContextManager

from allowances.types import Wallet, Transaction, FulfillmentRecord, SubjectRef, ObjectRef

class MyStore:
    def unit_of_work(self) -> AbstractAsyncContextManager[None]: ...

    # wallets
    async def lock_wallets(self, wallet_ids) -> list[Wallet]: ...
    async def find_wallets(self, *, subjects, wallet_types, currencies, at, lock=False) -> list[Wallet]: ...
    async def get_wallet(self, wallet_id, *, lock=False) -> Wallet | None: ...
    async def save_wallet(self, wallet) -> None: ...
    async def create_wallet(self, *, subject, wallet_type, currency, balance, **kwargs) -> Wallet: ...
    async def find_scheduled_wallets(self, *, subject, lock=False) -> list[Wallet]: ...
    async def find_expired_wallets(self, *, at, lock=False) -> list[Wallet]: ...
    async def archive_wallet(self, wallet) -> None: ...

    # ledger
    async def sum_transactions(self, *, wallet_id, since, until) -> Decimal: ...
    async def transactions_for_wallet(self, *, wallet_id, since, until) -> list[Transaction]: ...
    async def create_transaction(self, *, wallet_id, kind, amount, actor=None, metadata=None,
                                 **kwargs) -> Transaction: ...
    async def transactions_for_record(self, *, record_guid, lock=False) -> list[Transaction]: ...
    async def attach_object(self, *, transaction_ids, obj) -> None: ...

    # records
    async def get_or_create_record(self, guid=None) -> FulfillmentRecord: ...
    async def get_record(self, guid, *, lock=False) -> FulfillmentRecord | None: ...
    async def records_with_unattached_transactions(self, *, older_than=None) -> list[FulfillmentRecord]: ...

    # memberships
    async def add_membership(self, *, member, group) -> None: ...
    async def remove_membership(self, *, member, group) -> bool: ...
    async def memberships_of(self, *, members) -> list[tuple[SubjectRef, SubjectRef]]: ...
    async def lock_memberships(self) -> None: ...

    # modifiers
    async def find_modifier_factors(self, *, feature, subjects, at) -> list[Decimal]: ...
```

A store inherits no base class. `allowances.store.Store` is a `Protocol`: a class that implements the methods is a
store. It is `runtime_checkable`, so `isinstance(obj, Store)` works.

Methods take and return the frozen dataclasses of `allowances.types`, never storage rows.

## Execution

Every method except `unit_of_work` is a coroutine function (`async def`). `unit_of_work()` returns an asynchronous
context manager, for example a method decorated with `contextlib.asynccontextmanager`. The engine awaits nothing but
store methods: it starts no tasks and takes no locks of its own, so all concurrency control is the store's.

A store over a synchronous client (a DB-API driver, the Django ORM, `redis`) whose methods call that client directly
and await nothing that suspends serves both APIs: the asynchronous one, and [`allowances.sync`](/guide/synchronous),
which runs the engine's coroutines without an event loop. Such methods block the event loop while they run.

A store over an asynchronous client (asyncpg, `redis.asyncio`, adorm) suspends and serves only the asynchronous API;
`allowances.sync` raises `StoreSuspendedError` for it.

```python
from contextlib import asynccontextmanager


class MyStore:
    @asynccontextmanager
    async def unit_of_work(self):
        with self.connection.transaction():   # a synchronous client: never suspends
            yield

    async def get_wallet(self, wallet_id, *, lock=False):
        row = self.connection.fetch_one(...)
        return None if row is None else to_wallet(row)
```

## Requirements

**Ordering.** `find_wallets` returns oldest-first by `(created_at, id)`. The engine uses that order as the
tie-breaker within a fulfilment step; with an unstable order the same request charges different wallets on different
runs. A storage without a natural ordering keeps an insertion sequence. The in-memory backend does this, so that two
wallets created in the same microsecond sort deterministically.

**Atomicity.** `unit_of_work()` commits everything or nothing, and nests. The engine opens one; a caller may already
have opened another around it. A nested block belongs to the outer transaction, and when it raises, its own writes are
discarded and the outer block's are kept (a savepoint); an independent inner transaction is not correct. With this, an
operation that raises leaves no writes even when the caller catches the error inside its own block.

**Locking.** `lock_wallets` and a locked read (`lock=True` on `get_wallet`, `get_record`,
`transactions_for_record`) must prevent a concurrent operation from changing the same rows until the block ends.
Without it, two concurrent calls each read a balance of 10, each spend 10, and the wallet ends at −10 with no overdraft
configured. Single-threaded tests do not detect this.

**Lock order.** `lock_wallets(wallet_ids)` locks the given live wallets in ascending id order and returns their
current state in that order; a duplicated id comes back once, archived and unknown ids are left out. The engine passes
every wallet an operation uses in one call and locks no further wallet afterwards, so a backend that takes the locks in
that order cannot deadlock two operations against each other. Wallet ids of one store must be mutually comparable. On
a relational store this is `SELECT ... WHERE id IN (...) ORDER BY id FOR UPDATE`.

## Further requirements

**Wallets carry `source`, `allotment` and `scheduled`.** `create_wallet` receives all three, and `save_wallet`
persists `allotment` and `scheduled` along with `balance`, `first_overdraft_at`, `valid_from`, `valid_until`,
`overdraft_limit` and `reset_config`. `scheduled` is a JSON-safe dict the engine writes and reads; store it as is.
`find_scheduled_wallets` returns a subject's wallets whose `scheduled` is set, whatever their validity. `find_wallets`
takes `currencies=None` to mean every currency.

**The store filters by validity.** `find_wallets` returns only wallets valid at `at`: `valid_from` null or not after
`at`, `valid_until` null or after `at`. The engine does not re-check. `find_expired_wallets` returns wallets whose
`valid_until` is at or before `at`, so from its `valid_from` on a wallet is either valid or expired, never both.
`find_modifier_factors` uses the same interval: `valid_from` null or not after `at`, `valid_until` null or after `at`.

**`storno_of` must be unique.** A transaction may be reversed at most once. The constraint belongs in storage, not
in application code: a check in application code lets two concurrent reversals of one transaction both succeed and
refund twice. PostgreSQL and SQLite enforce it with a partial unique index; the in-memory backend scans and raises.

**`sum_transactions` runs on every fulfilment** against a [windowed wallet](/guide/sliding-windows). Index by
`(wallet_id, created_at)` or the equivalent.

**`archive_wallet` deletes the live rows.** It copies the wallet and its transactions to the store's archive, then
removes them. The archive format is up to the store; the core names no archive type. When reversing entries reference
the entries they reverse, the reversing rows are deleted first.

**`create_transaction` takes an explicit `at`.** Store it as `created_at` instead of the current time; sliding
windows and backfills depend on it. It also takes `actor`, a `SubjectRef` or `None`; store it and return it on every
`Transaction` read back, archived copies included.

**`create_transaction` takes `metadata`,** the application's `dict` or `None`. Store it unchanged, without reading it,
and return it on every `Transaction` read back, archived copies included; a store that keeps Python objects keeps a
copy, so a later change to the caller's `dict` does not reach the ledger.

**`records_with_unattached_transactions` looks at consumption only.** A record matches when it holds a
`CONSUMPTION` transaction with no object attached. Credits and transfers never make a record match.

**Memberships are direct pairs.** `add_membership` stores `(member, group)` at most once; `remove_membership` returns
whether the pair existed; `memberships_of(members=...)` returns the direct pairs whose member is any of `members`,
oldest first. The engine computes transitive groups and rejects cycles itself. `lock_memberships()` serialises
membership changes for the rest of the current unit of work on every database the backend supports: a row or table
lock, or nothing when `unit_of_work` already serialises every writer.

## Unsupported operations

```python
from allowances.errors import UnsupportedOperationError

async def records_with_unattached_transactions(self, *, older_than=None):
    raise UnsupportedOperationError("This store keeps no secondary index over transactions")
```

A method the store cannot implement raises `UnsupportedOperationError`; a no-op cannot be told apart from a working
implementation. Document which operations a partial backend supports.

## Testing a backend

The library's engine tests are written against the port, not against Django, and are coroutines (pytest-asyncio).
The `store` fixture in `tests/conftest.py` runs each of them once per store kind: `memory`, `django`, `django-sync`,
`adorm-django` and `adorm-asyncpg`. A kind added to `ALL_STORES` and to `new_store()` runs the whole suite against
another backend:

```python
def new_store(kind, worker=0):
    if kind == "mine":
        return MyStore()
    ...
```

The tests call the engine through `tests/api.py`, which awaits the asynchronous API. A store that never suspends can
also be tested through `allowances.sync` by wrapping it in `tests.api.ThreadedSyncStore`, as the `django-sync` kind
does.

A backend that passes them behaves like the shipped ones.
