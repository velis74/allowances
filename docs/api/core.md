# Core API

Everything in `allowances`. No dependencies — importing this pulls in nothing but the standard library.

Every engine function takes `store` first. If you're on Django you probably want [the shortcuts](/api/django#shortcuts) instead, which supply it for you.

## Engine

### `peek`

```python
peek(store, feature, quantity, subjects, provider, at=None) -> FulfillmentResult
```

Whether a request could be met. Runs the same planning walk as `fulfill` and writes nothing — no transactions, no fulfillment record, no balance changes.

The answer is a snapshot, not a reservation. A concurrent caller can spend what it just told you was available.

### `fulfill`

```python
fulfill(store, feature, quantity, subjects, provider,
        guid=None, allow_partial=False, at=None) -> FulfillmentResult
```

Consume the licence, writing one transaction per wallet drawn from.

| | |
| --- | --- |
| `feature` | Name of a feature the provider knows. |
| `quantity` | How many units. Anything `Decimal(str(...))` accepts. |
| `subjects` | `{name: SubjectRef}`. The names are what `fulfillment_order` refers to. |
| `guid` | Group this call with earlier ones under one record. `None` creates a fresh one. |
| `allow_partial` | Keep whatever was available instead of raising. |
| `at` | Treat this instant as "now" — validity windows, sliding windows, timestamps. |

Raises `InsufficientBalanceError` when the request can't be met in full and `allow_partial` is false. The whole call rolls back; the exception carries the result that *would* have been returned.

### `attach`

```python
attach(store, guid, obj: ObjectRef) -> None
```

Point every not-yet-attached transaction under `guid` at the domain object it paid for. Already-attached transactions are left alone. Raises `RecordNotFoundError` for an unknown guid.

### `unfulfill`

```python
unfulfill(store, guid, at=None) -> None
```

Reverse everything under `guid` by writing matching storno entries. Nothing is deleted or edited.

Idempotent — an already-reversed transaction is skipped. Transactions whose wallet has since been archived are skipped too. An unknown guid raises `RecordNotFoundError`.

### `plan_switch`

```python
plan_switch(store, subjects, open_wallets: Sequence[WalletSpec],
            effective_date=None) -> list[Wallet]
```

Close every wallet these subjects hold and open the given set instead, atomically. Each spec is opened once per subject. A future `effective_date` schedules the switch rather than applying it. Returns the wallets opened.

### `archive_expired_wallets`

```python
archive_expired_wallets(store, at=None) -> int
```

Move wallets past their `valid_until` into the archive. Returns how many. Meant for a periodic job — expired wallets are invisible to fulfillment, so nothing else will ever reach them.

### `find_unattached_fulfillment_records`

```python
find_unattached_fulfillment_records(store, older_than=None) -> list[FulfillmentRecord]
```

Records holding a transaction that never got an object attached — quota spent on something that may never have happened. Pass `older_than` so in-flight operations aren't swept up.

### `effective_balance`

```python
effective_balance(store, wallet, at=None) -> Decimal
```

The balance that governs decisions. For an ordinary wallet, its stored balance. For a [windowed](/guide/sliding-windows) one, the allotment plus every transaction inside the current window.

## Types

All frozen dataclasses, from `allowances.types`.

### `SubjectRef`

```python
SubjectRef(type: str, id) -> SubjectRef
SubjectRef.parse("auth.user:42")
```

Whoever holds a licence. `id` is coerced to `str`, so integer, UUID and slug keys all work. `type` is an opaque string a backend maps back to a real object; the Django adapter uses `"app_label.modelname"`.

### `ObjectRef`

Same shape, for the object a fulfillment paid for.

### `Wallet`

`id`, `subject`, `wallet_type`, `currency`, `balance`, `created_at`, `valid_from`, `valid_until`, `overdraft_limit`, `first_overdraft_at`, `reset_config`.

`is_valid_at(at)` — whether fulfillment can see it.

### `Transaction`

`id`, `wallet_id`, `kind`, `amount` (signed), `created_at`, `record_guid`, `feature`, `storno_of_id`, `obj`.

### `WalletSpec`

```python
WalletSpec(wallet_type, currency, balance=Decimal("0"),
           overdraft_limit=None, reset_config=None, valid_until=None)
```

A wallet to open, without saying for whom. Used by `plan_switch`.

### `FulfillmentResult`

`requested`, `granted`, `satisfied`, `transactions`, `guid`, `modifier_applied`.

`modifier_applied` is `None` when no modifier changed the cost.

### `WalletType` / `TransactionKind`

`WalletType.GRANT` / `CONSUMABLE`. `TransactionKind.GRANT` / `CONSUMPTION` / `STORNO`. String enums, so they compare equal to their values.

## Definitions

### `DefinitionProvider`

```python
class DefinitionProvider(abc.ABC):
    def get_feature(self, name: str) -> FeatureDefinition: ...
    def get_currency(self, name: str) -> CurrencyDefinition: ...
```

Two methods. Implement them against a dict, a settings module, a remote service — the engine doesn't distinguish.

### `DictProvider`

```python
DictProvider(config: dict | None = None)
```

Reads the [configuration shape](/guide/concepts) from a dict. The Django adapter's `SettingsProvider` subclasses this to read `settings.ALLOWANCES`.

### `FeatureDefinition`

`name`, `grant`, `consumable`, `subjects`, `fulfillment_order`, `overdraft`.

`section_for(wallet_type)` returns the matching `FeatureSection` or `None`.

### `FeatureSection`

`currencies` — a tuple of `CurrencySpec`. `rate_for(currency)` returns the conversion rate or `None`; `currency_names` lists them.

### `CurrencyDefinition`

`name`, `general`, `round_fn`.

## Store

The storage port. See [How the port works](/backends/) for the obligations and [Writing a backend](/backends/writing-a-backend) for the full method list.

`allowances.store.Store` is a `runtime_checkable` `Protocol` — there is nothing to inherit.

## Errors

| | |
| --- | --- |
| `AllowancesError` | Base class for everything below. |
| `InsufficientBalanceError` | `fulfill` couldn't cover the request. Carries `.result`. |
| `DefinitionNotFoundError` | Unknown feature or currency. Also a `KeyError`. |
| `RecordNotFoundError` | No fulfillment record with that guid. Also a `LookupError`. |
| `UnsupportedOperationError` | A backend deliberately doesn't implement this. Also a `NotImplementedError`. |

## Windows

```python
window_start(reset_config: dict, at: datetime) -> datetime
```

When the window containing `at` began. See [Sliding windows](/guide/sliding-windows) for the config shapes.
