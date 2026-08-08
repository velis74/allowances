# Writing a backend

Fifteen methods. The two that ship — [Django](/backends/django) and [in-memory](/backends/memory) — are worth reading as worked examples; `allowances/backends/memory.py` is about 250 lines and implements everything.

## The shape

```python
from allowances.types import Wallet, Transaction, FulfillmentRecord, SubjectRef, ObjectRef

class MyStore:
    def unit_of_work(self): ...

    # wallets
    def find_wallets(self, *, subjects, wallet_types, currencies, at, lock=False) -> list[Wallet]: ...
    def get_wallet(self, wallet_id, *, lock=False) -> Wallet | None: ...
    def save_wallet(self, wallet) -> None: ...
    def create_wallet(self, *, subject, wallet_type, currency, balance, **kwargs) -> Wallet: ...
    def close_wallets(self, *, subject, at) -> int: ...
    def find_expired_wallets(self, *, at, lock=False) -> list[Wallet]: ...
    def archive_wallet(self, wallet) -> None: ...

    # ledger
    def sum_transactions(self, *, wallet_id, since, until) -> Decimal: ...
    def create_transaction(self, *, wallet_id, kind, amount, **kwargs) -> Transaction: ...
    def transactions_for_record(self, *, record_guid, lock=False) -> list[Transaction]: ...
    def attach_object(self, *, transaction_ids, obj) -> None: ...

    # records
    def get_or_create_record(self, guid=None) -> FulfillmentRecord: ...
    def get_record(self, guid, *, lock=False) -> FulfillmentRecord | None: ...
    def records_with_unattached_transactions(self, *, older_than=None) -> list[FulfillmentRecord]: ...

    # modifiers
    def find_modifier_factors(self, *, feature, subjects, at) -> list[Decimal]: ...
```

No base class to inherit. `allowances.store.Store` is a `Protocol` — implement the methods and you're a store. It is `runtime_checkable` if you want an assertion.

Nothing returns your own types. Everything crossing the boundary is one of the frozen dataclasses in `allowances.types`, so the engine never touches your rows.

## The three things that must be right

**Ordering.** `find_wallets` returns oldest-first by `(created_at, id)`. The engine uses that as the tie-breaker within a fulfillment step, so an unstable order means the same request charges different wallets on different runs. If your storage has no natural ordering, keep an insertion sequence — the in-memory backend does exactly this, because two wallets created in the same microsecond would otherwise sort arbitrarily.

**Atomicity.** `unit_of_work()` commits everything or nothing, and nests. The engine opens one; a caller may already have opened another around it. Joining the outer one is correct; starting a genuinely independent inner transaction is not.

**Locking.** `lock=True` must prevent a concurrent fulfillment from seeing the same rows until the block ends. Get this wrong and two simultaneous calls each read a balance of 10, each spend 10, and the wallet ends at −10 with no overdraft configured. It will not show up in testing.

## Smaller details that bite

**Validity filtering belongs to you.** `find_wallets` returns only wallets valid at `at`: `valid_from` null or past, `valid_until` null or future. The engine does not re-check.

**`storno_of` must be unique.** A transaction may be reversed at most once. Enforce it in storage, not in application code — a lost race here silently refunds twice. Postgres and SQLite both do it with a partial unique index; the in-memory backend scans and raises.

**`sum_transactions` is on the hot path.** Every fulfillment against a [windowed wallet](/guide/sliding-windows) calls it. Index by `(wallet_id, created_at)` or the equivalent.

**`archive_wallet` deletes the live rows.** Copy the wallet and its transactions wherever your archive lives, then remove them. What the archive looks like is entirely yours — the core never names an archive type. Mind referential order: if reversing entries point at the entries they reverse, the reversing rows have to go first.

**`create_transaction` takes an explicit `at`.** Honour it rather than stamping your own clock. Sliding windows and backfills both depend on it.

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
