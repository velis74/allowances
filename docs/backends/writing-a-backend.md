# Writing a backend

Twenty-two methods. The two that ship — [Django](/backends/django) and [in-memory](/backends/memory) — are worth reading as worked examples; `allowances/backends/memory.py` is about 330 lines and implements everything.

## The shape

```python
from allowances.types import Wallet, Transaction, FulfillmentRecord, SubjectRef, ObjectRef

class MyStore:
    def unit_of_work(self): ...

    # wallets
    def lock_wallets(self, wallet_ids) -> list[Wallet]: ...
    def find_wallets(self, *, subjects, wallet_types, currencies, at, lock=False) -> list[Wallet]: ...
    def get_wallet(self, wallet_id, *, lock=False) -> Wallet | None: ...
    def save_wallet(self, wallet) -> None: ...
    def create_wallet(self, *, subject, wallet_type, currency, balance, **kwargs) -> Wallet: ...
    def find_scheduled_wallets(self, *, subject, lock=False) -> list[Wallet]: ...
    def find_expired_wallets(self, *, at, lock=False) -> list[Wallet]: ...
    def archive_wallet(self, wallet) -> None: ...

    # ledger
    def sum_transactions(self, *, wallet_id, since, until) -> Decimal: ...
    def transactions_for_wallet(self, *, wallet_id, since, until) -> list[Transaction]: ...
    def create_transaction(self, *, wallet_id, kind, amount, actor=None, **kwargs) -> Transaction: ...
    def transactions_for_record(self, *, record_guid, lock=False) -> list[Transaction]: ...
    def attach_object(self, *, transaction_ids, obj) -> None: ...

    # records
    def get_or_create_record(self, guid=None) -> FulfillmentRecord: ...
    def get_record(self, guid, *, lock=False) -> FulfillmentRecord | None: ...
    def records_with_unattached_transactions(self, *, older_than=None) -> list[FulfillmentRecord]: ...

    # memberships
    def add_membership(self, *, member, group) -> None: ...
    def remove_membership(self, *, member, group) -> bool: ...
    def memberships_of(self, *, members) -> list[tuple[SubjectRef, SubjectRef]]: ...
    def lock_memberships(self) -> None: ...

    # modifiers
    def find_modifier_factors(self, *, feature, subjects, at) -> list[Decimal]: ...
```

No base class to inherit. `allowances.store.Store` is a `Protocol` — implement the methods and you're a store. It is `runtime_checkable` if you want an assertion.

Nothing returns your own types. Everything crossing the boundary is one of the frozen dataclasses in `allowances.types`, so the engine never touches your rows.

## The four things that must be right

**Ordering.** `find_wallets` returns oldest-first by `(created_at, id)`. The engine uses that as the tie-breaker within a fulfillment step, so an unstable order means the same request charges different wallets on different runs. If your storage has no natural ordering, keep an insertion sequence — the in-memory backend does exactly this, because two wallets created in the same microsecond would otherwise sort arbitrarily.

**Atomicity.** `unit_of_work()` commits everything or nothing, and nests. The engine opens one; a caller may already have opened another around it. A nested block belongs to the outer transaction, and when it raises, its own writes are discarded and the outer block's are kept (a savepoint); starting a genuinely independent inner transaction is not correct. The engine relies on this: an operation that raises leaves nothing behind even when the caller catches the error inside its own block.

**Locking.** `lock_wallets` and a locked read (`lock=True` on `get_wallet`, `get_record`, `transactions_for_record`) must prevent a concurrent operation from changing the same rows until the block ends. Get this wrong and two simultaneous calls each read a balance of 10, each spend 10, and the wallet ends at −10 with no overdraft configured. It will not show up in testing.

**Lock order.** `lock_wallets(wallet_ids)` locks the given live wallets in ascending id order and returns their current state in that order; a duplicated id comes back once, archived and unknown ids are left out. The engine passes every wallet an operation uses in one call and locks no further wallet afterwards, so a backend that takes the locks in that order cannot deadlock two operations against each other. Wallet ids of one store must therefore be mutually comparable. On a relational store that is `SELECT ... WHERE id IN (...) ORDER BY id FOR UPDATE`.

## Smaller details that bite

**Wallets carry `source`, `allotment` and `scheduled`.** `create_wallet` receives all three, and `save_wallet` persists `allotment` and `scheduled` along with `balance`, `first_overdraft_at`, `valid_from`, `valid_until`, `overdraft_limit` and `reset_config`. `scheduled` is a JSON-safe dict the engine writes and reads; store it as is. `find_scheduled_wallets` returns a subject's wallets whose `scheduled` is set, whatever their validity. `find_wallets` takes `currencies=None` to mean every currency.

**Validity filtering belongs to you.** `find_wallets` returns only wallets valid at `at`: `valid_from` null or past, `valid_until` null or future. The engine does not re-check.

**`storno_of` must be unique.** A transaction may be reversed at most once. Enforce it in storage, not in application code — a lost race here silently refunds twice. Postgres and SQLite both do it with a partial unique index; the in-memory backend scans and raises.

**`sum_transactions` is on the hot path.** Every fulfillment against a [windowed wallet](/guide/sliding-windows) calls it. Index by `(wallet_id, created_at)` or the equivalent.

**`archive_wallet` deletes the live rows.** Copy the wallet and its transactions wherever your archive lives, then remove them. What the archive looks like is entirely yours — the core never names an archive type. Mind referential order: if reversing entries point at the entries they reverse, the reversing rows have to go first.

**`create_transaction` takes an explicit `at`.** Honour it rather than stamping your own clock. Sliding windows and backfills both depend on it. It also takes `actor`, a `SubjectRef` or `None`; store it and return it on every `Transaction` read back, archived copies included.

**`records_with_unattached_transactions` looks at consumption only.** A record matches when it holds a `CONSUMPTION` transaction with no object attached. Credits and transfers never make a record match.

**Memberships are direct pairs.** `add_membership` stores `(member, group)` at most once; `remove_membership` returns whether the pair existed; `memberships_of(members=...)` returns the direct pairs whose member is any of `members`, oldest first. The engine computes transitive groups and rejects cycles itself. `lock_memberships()` serialises membership changes for the rest of the current unit of work — an advisory or table lock, or nothing when `unit_of_work` already serialises every writer.

## When you can't implement something

```python
from allowances.errors import UnsupportedOperationError

def records_with_unattached_transactions(self, *, older_than=None):
    raise UnsupportedOperationError("This store keeps no secondary index over transactions")
```

Better than a silent no-op, which looks identical to a working implementation until the day it matters. Document which operations a partial backend supports.

## Testing yours

Point this library's own engine tests at it. They are written against the port, not against Django — add a parameter to the `store` fixture in `tests/conftest.py` and the whole suite runs against your backend:

```python
@pytest.fixture(params=["memory", "django", "mine"])
def store(request):
    ...
```

If they pass, your backend behaves like the ones that ship. That is the entire point of having written them that way.
