# Core API

Everything in `allowances`. It depends only on the standard library.

Every engine function takes `store` first and is a coroutine function: its call is awaited. The engine awaits nothing
but store methods. Types, definitions, errors and `window_start` are plain. On Django,
[the shortcuts](/api/django#shortcuts) supply the store and the provider. [`allowances.sync`](#allowances-sync) has
every engine function as a plain function.

## Engine

### `peek`

```python
async def peek(store, feature, quantity, subjects, provider, at=None) -> FulfillmentResult
```

Whether a request could be met. Runs the same planning walk as `fulfill`, with `subjects` expanded by their groups, and
writes nothing — no transactions, no fulfilment record, no balance changes. A [right](/guide/permissions) held by any
expanded subject makes the answer satisfied.

`peek` reserves nothing: a concurrent call can spend the balance it reports as available.

### `fulfill`

```python
async def fulfill(store, feature, quantity, subjects, provider,
                  guid=None, allow_partial=False, at=None, *, actor=None,
                  metadata: dict | None = None) -> FulfillmentResult
```

Consume the licence, writing one transaction per wallet drawn from. A wallet whose effective rate is 0 and whose balance
is positive covers the rest of the request; its use is a `CONSUMPTION` entry of amount 0 and its balance is unchanged.

| | |
| --- | --- |
| `feature` | Name of a feature the provider knows. |
| `quantity` | How many units. Anything `Decimal(str(...))` accepts. |
| `subjects` | `{name: SubjectRef}`. The names are what `fulfillment_order` refers to. Each subject is expanded with its transitive groups under the same name — see [Memberships](/guide/fulfillment-order#memberships). |
| `guid` | Group this call with earlier ones under one record. `None` creates a fresh one. |
| `allow_partial` | Keep whatever was available instead of raising. |
| `at` | Treat this instant as "now" — validity windows, sliding windows, timestamps. |
| `actor` | `SubjectRef` recorded on every entry. `None` records the only subject of `subjects` when it holds exactly one, otherwise no actor. The actor is the caller's subject, never a group it was expanded to. |
| `metadata` | The application's data recorded on every consumption entry of this call, e.g. `{"reason": "newsletter", "ticket": 123}`. Stored as given, JSON-serialisable; the library does not read it. |

Every candidate wallet is locked in one `lock_wallets` call before balances are read; an existing record named by `guid`
is locked first. See [Lock order](#lock-order).

Raises `InsufficientBalanceError` when the request cannot be met in full and `allow_partial` is false. The whole call
rolls back; the exception carries the result the call would have returned.

### `attach`

```python
async def attach(store, guid, obj: ObjectRef) -> None
```

Point every not-yet-attached transaction under `guid` at the domain object it paid for. Already-attached transactions
are left alone. Raises `RecordNotFoundError` for an unknown guid.

### `unfulfill`

```python
async def unfulfill(store, guid, at=None, *, actor=None) -> None
```

Reverse everything under `guid` — consumption, credits and transfers — by writing matching storno entries, each recorded
with `actor` (who reversed it) and a copy of the reversed entry's `metadata`. Nothing is deleted or edited. A storno of
a zero entry is zero.

Idempotent — an already-reversed transaction is skipped. Transactions whose wallet has since been archived are skipped
too. An unknown guid raises `RecordNotFoundError`.

Reversing a transfer takes the amount back from its target. The check nets all of the record's stornos on the target, so
consumption of the transferred amount under the same guid is given back first. When the target would end below zero
(`max(effective_balance, 0)` plus that net change is negative) or has been archived, `InsufficientBalanceError` is
raised and nothing is written. When the transfer's source has been archived, `WalletNotFoundError` is raised and nothing
is written.

The record is locked first, then every wallet its transactions touch, in one `lock_wallets` call.

### `plan_switch`

```python
async def plan_switch(store, subjects, open_wallets: Sequence[WalletSpec],
                      effective_date=None) -> list[Wallet]
```

Move these subjects onto the plan described by `open_wallets`, atomically. Only wallets with `source == "plan"` take
part. A spec with `on_switch` `ADJUST` or `TOP_UP` keeps the subject's current plan wallet of the same type and currency
and adjusts it; every other current plan wallet is closed at `effective_date` and the remaining specs are opened. Each
spec is applied once per subject. Returns the resulting wallets, kept or opened, per subject in spec order.

A future `effective_date` schedules the switch: closing and opening take effect through validity dates, adjustments
apply from that date. A pending switch for the same subject is cancelled first. See [Plan
switch](/guide/plans-and-archival#plan-switch) and [Switch policies](/guide/plans-and-archival#switch-policies).

### `cancel_scheduled_switch`

```python
async def cancel_scheduled_switch(store, subjects) -> int
```

Cancel the not-yet-effective switch of these subjects: closed wallets get their previous `valid_until` back, scheduled
adjustments are dropped, wallets it would have opened end before they begin. Returns how many subjects had a switch
cancelled.

### `open_wallet`

```python
async def open_wallet(store, subject: SubjectRef, spec: WalletSpec, *, source: str,
                      effective_date=None) -> Wallet
```

Open one wallet, valid from `effective_date` (default now). `source` is stored on it; a wallet whose source is not
`"plan"` is never closed or adjusted by `plan_switch`. A spec with `on_switch` `ADJUST` or `TOP_UP` records its balance
as the wallet's `allotment`.

### `credit`

```python
async def credit(store, wallet_id, amount, *, guid=None, obj: ObjectRef | None = None,
                 actor: SubjectRef | None = None, at=None,
                 metadata: dict | None = None) -> Transaction
```

Add a positive `amount` to a wallet's balance, as a `GRANT` transaction recorded with `actor` and `metadata`. `obj`
attaches it to whatever paid for it. `guid` files it under a fulfilment record so `unfulfill(guid)` reverses it; an
existing record is locked before the wallet.

Raises `WalletNotFoundError` for an unknown or archived wallet, `UnsuitableWalletError` for a windowed wallet or one not
valid at `at`, `ValueError` for an amount that is not positive.

### `transfer`

```python
async def transfer(store, from_wallet_id, to_wallet_id, amount, *, guid=None,
                   obj: ObjectRef | None = None, actor: SubjectRef | None = None,
                   at=None, metadata: dict | None = None) -> FulfillmentResult
```

Move a positive `amount` from one wallet to another of the same currency. Writes two `TRANSFER` transactions under one
fulfilment record (`guid`, or a new one): a debit of `-amount` on the source and a credit of `amount` on the target,
both recorded with `actor` and `metadata` and attached to `obj` when given. The wallet types may differ. A windowed
wallet's stored allotment is not changed; the entries count in its window like any other. A source drained to zero is
not archived.

The source must hold the amount — `max(effective_balance, 0) >= amount`; overdraft is never used. Both wallets are
locked in one `lock_wallets` call; an existing record named by `guid` is locked first.

Returns a `FulfillmentResult` with `requested == granted == amount`, `satisfied=True`, `transactions=[debit, credit]`
and the record's `guid`. `unfulfill(guid)` reverses both legs.

Raises `ValueError` for an amount that is not positive, `WalletNotFoundError` for an unknown or archived wallet,
`UnsuitableWalletError` for the same wallet on both sides, different currencies or a wallet not valid at `at`, and
`InsufficientBalanceError` when the source does not hold the amount (its `result.granted` is what the source holds).
Nothing is written when it raises.

### `close_wallet`

```python
async def close_wallet(store, wallet_id, *, at=None) -> Wallet
```

End a wallet at `at` (default now) — revoke a right, close a budget. A scheduled plan-switch change already due at `at`
is applied first. `valid_until` becomes `at` unless the wallet already ends earlier, and a scheduled switch record not
yet effective is dropped. No ledger entry is written and the balance is unchanged; `archive_expired_wallets` archives
the wallet later. Returns the closed wallet. Raises `WalletNotFoundError` for an unknown or archived wallet.

### `adjust_wallet`

```python
async def adjust_wallet(store, wallet_id, allotment, *, at=None) -> Wallet
```

Give a wallet that has an `allotment` a new one, with `ADJUST` arithmetic: a windowed wallet gets the new per-window
allotment, a stored balance moves by the difference (booked as a `GRANT` transaction, possibly negative). Raises
`WalletNotFoundError` or, for a wallet without an allotment, `UnsuitableWalletError`.

### `consumed`

```python
async def consumed(store, wallet_id, since, until) -> Decimal
```

Net amount drawn from a wallet in `[since, until]`: consumption minus reversals of consumption booked in that interval.
Credits, transfers, adjustments and their reversals are not counted. The figure to bill for a [postpaid
budget](/guide/credits-and-budgets#postpaid-budget).

### `archive_expired_wallets`

```python
async def archive_expired_wallets(store, at=None) -> int
```

Move wallets whose `valid_until` is at or before `at` into the archive; a wallet is valid until, not including, its
`valid_until`.
Returns how many. Run it from a periodic job: fulfilment
ignores expired wallets and no other operation archives them.

### `find_unattached_fulfillment_records`

```python
async def find_unattached_fulfillment_records(store, older_than=None) -> list[FulfillmentRecord]
```

Records holding a `CONSUMPTION` transaction without an attached object; the operation that consumed it may have failed.
Credit and transfer entries never make a record match. `older_than` limits the result to records created at or before
that instant, which excludes operations still in progress.

### `transactions_for_object`

```python
async def transactions_for_object(store, obj) -> list[Transaction]
```

The ledger entries of one domain object (`ObjectRef`), oldest first: every live entry attached to it by `attach`,
`credit(obj=)` or `transfer(obj=)`, and every storno that reverses one of them. Stornos carry no object of their own;
they are found through `storno_of_id` within the entries' fulfilment records. The sum of `amount` over the result is the
object's net effect on the ledger: consumption is negative, its storno gives it back. See [Entries of an
object](/guide/reversals#entries-of-an-object).

A storno that reverses an entry of another object is left out. Entries of archived wallets are not returned. An unknown
object gives `[]`. Raises `UnsupportedOperationError` when the store does not implement the optional
`transactions_for_object` (see [Store](#store)).

### `add_member`

```python
async def add_member(store, member: SubjectRef, group: SubjectRef) -> None
```

Make `member` a direct member of `group`. A group is any subject; groups may be members of groups. An existing
membership is left as it is. Membership changes are serialised with `Store.lock_memberships()`, so two concurrent
additions cannot form a cycle together. Raises `MembershipCycleError` when `member` is `group` or `group` is already,
directly or transitively, a member of `member`.

### `remove_member`

```python
async def remove_member(store, member: SubjectRef, group: SubjectRef) -> bool
```

Remove the direct membership of `member` in `group`. Returns whether it existed. Memberships through other groups are
unaffected.

### `groups_of`

```python
async def groups_of(store, subject: SubjectRef) -> list[SubjectRef]
```

Every group `subject` belongs to, directly or through other groups: breadth-first, each group once. Direct groups come
first, in membership creation order; each further depth lists the groups of the previous depth's members in that depth's
order, each member's groups in creation order. `subject` itself is not included. Terminates on cyclic data.

### `effective_balance`

```python
async def effective_balance(store, wallet, at=None) -> Decimal
```

The balance that governs decisions. For an ordinary wallet, its stored balance. For a [windowed](/guide/sliding-windows)
one, the allotment plus every transaction inside the current window.

### `effective_allowances`

```python
async def effective_allowances(store, subject: SubjectRef, *, at=None, wallet_types=None,
                               currencies=None) -> list[Allowance]
```

What `subject` holds at `at` (default now): every live wallet of the subject and of every group in `groups_of(subject)`,
of any currency and wallet type, each as an [`Allowance`](#allowance) with its holder, depth and effective balance. See
[What a subject holds](/guide/concepts#what-a-subject-holds).

| | |
| --- | --- |
| `wallet_types` | Iterable of `WalletType`. `None` reads both `GRANT` and `CONSUMABLE`. |
| `currencies` | Iterable of currency names. `None` reads every currency. |

The wallets of all those subjects are read in one `find_wallets` call, and only wallets valid at `at` are kept. A wallet
with a scheduled plan-switch change due at `at` is returned with the change applied, without writing it. Wallets of
every balance are listed, zero and negative included; filter on `balance` to keep what is held. The list is ordered by
`depth`, then by the holder's position in `groups_of`, then in the store's wallet order.

Locks nothing and writes nothing; like `peek`, it reserves nothing.

### Lock order

`fulfill`, `unfulfill`, `transfer`, `close_wallet`, `plan_switch`, `cancel_scheduled_switch` and
`archive_expired_wallets` lock every wallet they use in one `Store.lock_wallets` call, which takes the locks in
ascending wallet id, before reading the balances they rely on, and lock no further wallet afterwards. `credit` and
`adjust_wallet` lock their one wallet with `get_wallet(..., lock=True)`. A fulfilment record is locked before its
wallets. Two operations therefore never wait for each other in a cycle.

## Types

All frozen dataclasses, from `allowances.types`.

### `SubjectRef`

```python
SubjectRef(type: str, id) -> SubjectRef
SubjectRef.parse("auth.user:42")
```

The holder of a wallet. `id` is converted to `str`; integer, UUID and slug primary keys are supported. `type` is an
opaque string a backend maps back to a real object; the Django adapter uses `"app_label.modelname"`.

### `ObjectRef`

Same shape, for the object a fulfilment paid for.

### `Wallet`

`id`, `subject`, `wallet_type`, `currency`, `balance`, `created_at`, `valid_from`, `valid_until`, `overdraft_limit`,
`first_overdraft_at`, `reset_config`, `source` (default `"plan"`, the value of `PLAN_SOURCE`), `allotment` (`None`
unless opened with an `ADJUST` or `TOP_UP` policy), `scheduled` (what a not-yet-effective plan switch changes on it;
written by the engine).

`is_valid_at(at)` — whether fulfilment can see it.

### `Transaction`

`id`, `wallet_id`, `kind`, `amount` (signed), `created_at`, `record_guid`, `feature`, `storno_of_id`, `obj`, `actor`,
`metadata`.

`amount` is negative for consumption and for the debit leg of a transfer, positive for a grant and for the credit leg of
a transfer, zero for the use of a right; a storno carries the negated amount of the entry it reverses. `actor` is the
`SubjectRef` on whose behalf the entry was made, or `None`. `metadata` is the application's `dict` given to `fulfill`,
`credit` or `transfer`, or `None`; a storno carries a copy of the reversed entry's, and plan adjustments carry none. The
library neither prescribes nor reads its keys and does not validate it. It must be JSON-serialisable: the Django and
adorm stores keep it in a JSON column. `MemoryStore` keeps a deep copy, so changing the caller's `dict` afterwards does
not change the entry.

### `WalletSpec`

```python
WalletSpec(wallet_type, currency, balance=Decimal("0"),
           overdraft_limit=None, reset_config=None, valid_until=None,
           on_switch=SwitchPolicy.REPLACE)
```

A wallet to open, without saying for whom. Used by `plan_switch` and `open_wallet`. `on_switch` is what a plan switch
does with the subject's existing plan wallet of the same type and currency.

### `SwitchPolicy`

`REPLACE` closes the existing wallet and opens a new one. `ADJUST` keeps it and moves its balance by *new − old
allotment*, possibly below zero. `TOP_UP` keeps it and adds only a positive difference; both record the new allotment.
On a windowed wallet `ADJUST` and `TOP_UP` both set the per-window allotment to the new amount. A wallet without an
allotment, or one whose windowed-ness differs from the spec's, is replaced.

### `Allowance`

One wallet a subject holds, directly or through a group, as `effective_allowances` returns it.

| Field | Type | |
| --- | --- | --- |
| `wallet` | `Wallet` | the wallet as it is at `at`, with a due scheduled change applied |
| `holder` | `SubjectRef` | the wallet's owner: the queried subject or one of its groups |
| `depth` | `int` | 0 for the subject itself, 1 for a direct group, 2 for a group of a direct group, … |
| `balance` | `Decimal` | `effective_balance` of the wallet at `at`; for a windowed wallet, the balance left in the current window |

### `FulfillmentResult`

`requested`, `granted`, `satisfied`, `transactions`, `guid`, `modifier_applied`.

`modifier_applied` is the factor used, the smallest of the modifiers that apply; `None` when no modifier changed the
cost.

### `WalletType` / `TransactionKind`

`WalletType.GRANT` / `CONSUMABLE`. `TransactionKind.GRANT` / `CONSUMPTION` / `TRANSFER` / `STORNO`. String enums, so
they compare equal to their values. A `GRANT` transaction is a credit or a balance change made by a plan adjustment; a
`TRANSFER` transaction is one leg of `transfer`.

## Definitions

### `DefinitionProvider`

```python
class DefinitionProvider(abc.ABC):
    def get_feature(self, name: str) -> FeatureDefinition: ...
    def get_currency(self, name: str) -> CurrencyDefinition: ...
```

An implementation may read definitions from a dict, a settings module or a remote service.

### `DictProvider`

```python
DictProvider(config: dict | None = None)
```

Reads the [configuration shape](/guide/concepts) from a dict. The Django adapter's `SettingsProvider` subclasses this to
read `settings.ALLOWANCES`.

### `FeatureDefinition`

`name`, `grant`, `consumable`, `subjects`, `fulfillment_order`, `overdraft`.

`fulfillment_order` is a list of `(wallet_type, subject_name, source)` steps — `DictProvider` parses two-element steps
and bare strings into this shape with `None` for the missing parts — or a callable.

`section_for(wallet_type)` returns the matching `FeatureSection` or `None`.

### `FeatureSection`

`currencies` — a tuple of `CurrencySpec`. `rate_for(currency)` returns the conversion rate or `None`; `currency_names`
lists them.

### `CurrencyDefinition`

`name`, `general`, `round_fn`.

## Store

The storage port. See [How the port works](/backends/) for the obligations, including the lock order and comparable
wallet ids, and [Writing a backend](/backends/writing-a-backend) for the full method list.

`allowances.store.Store` is a `runtime_checkable` `Protocol` — there is nothing to inherit. Every method except
`unit_of_work` is a coroutine function; `unit_of_work()` returns an asynchronous context manager
(`async with store.unit_of_work():`). A store whose methods never suspend also serves `allowances.sync`.

`transactions_for_object(*, obj)` is an optional store method, not a member of the protocol, so
`isinstance(store, Store)` does not depend on it. Without it, the engine function
[`transactions_for_object`](#transactions-for-object) raises `UnsupportedOperationError`; every other function works.
All stores shipped with the library implement it.

## `allowances.sync`

The synchronous API; see [Synchronous API](/guide/synchronous).

Every engine function above, with the same name, signature and docstring, as a plain function that returns what the
coroutine returns: `add_member`, `adjust_wallet`, `archive_expired_wallets`, `attach`, `cancel_scheduled_switch`,
`close_wallet`, `consumed`, `credit`, `effective_allowances`, `effective_balance`,
`find_unattached_fulfillment_records`, `fulfill`, `groups_of`, `open_wallet`, `peek`, `plan_switch`, `remove_member`,
`transactions_for_object`, `transfer`, `unfulfill`. The types, definitions, errors, `Store` and `window_start` are
re-exported, the same objects as in `allowances`.

The store passed must never suspend; a call that suspends raises `StoreSuspendedError`.

### `unit_of_work`

```python
with unit_of_work(store): ...
```

The store's `unit_of_work()` as a synchronous context manager. Every call inside the block runs in its transaction;
blocks nest with the store's savepoint semantics.

### `allowances.sync.driver`

```python
run(coroutine) -> T
synchronous(function, module="allowances.sync") -> Callable
```

`run` runs a coroutine to completion on the calling thread, without an event loop, and returns its result; an exception
raised by the coroutine propagates unchanged. A coroutine that suspends is closed and `StoreSuspendedError` is raised.
`synchronous(function, module)` returns a plain function with the signature and docstring (`functools.wraps`) of the
coroutine function `function`, whose call runs the coroutine with `run`. Its `__module__` is `module`: a wrapper bound
under the coroutine function's name in that module is found by `pickle`, so it can be passed to a process pool.

## Errors

| | |
| --- | --- |
| `AllowancesError` | Base class for everything below. |
| `InsufficientBalanceError` | `fulfill` cannot cover the request, a `transfer` source does not hold the amount, or `unfulfill` cannot take a transfer back from its target. Carries `.result`. |
| `DefinitionNotFoundError` | Unknown feature or currency. Also a `KeyError`. |
| `RecordNotFoundError` | No fulfilment record with that guid. Also a `LookupError`. |
| `WalletNotFoundError` | No live wallet with that id, or `unfulfill` of a transfer whose source has been archived. Also a `LookupError`. |
| `UnsuitableWalletError` | The wallet does not accept the operation: `credit` on a windowed wallet or one not valid now, `adjust_wallet` without an allotment, `transfer` between different currencies, from a wallet to itself, or with a wallet not valid now. Also a `ValueError`. |
| `MembershipCycleError` | `add_member` would make a subject a member of itself, directly or through other groups. |
| `UnsupportedOperationError` | The backend does not implement this operation. Also a `NotImplementedError`. |
| `StoreSuspendedError` | A call through `allowances.sync` suspended: the store awaited something that yields to an event loop. Also a `RuntimeError`. `allowances_django.store.DjangoStore` raises it when called without a running event loop. |

## Windows

```python
window_start(reset_config: dict, at: datetime) -> datetime
```

When the window containing `at` began. See [Sliding windows](/guide/sliding-windows) for the config shapes.
