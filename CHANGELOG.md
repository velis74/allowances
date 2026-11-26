# Changelog

## 0.5.0 — 2026-11-26

Upgrade steps: [Upgrading to 0.5](https://docs.velis.si/allowances/guide/upgrading#upgrading-to-0-5).

### Added

- `Transaction.metadata`: the application's own JSON-serialisable `dict` recorded with a ledger entry; the library
  neither prescribes nor reads its keys. `fulfill` records `metadata=` on every consumption entry of the call, `credit`
  on its grant entry and `transfer` on both legs; `unfulfill` copies the reversed entry's metadata onto each storno.
  Archival keeps it.
- Django: `metadata` (`JSONField`, null) on `Transaction` and `TransactionArchive`, migration
  `0005_transaction_metadata`; `metadata=` on the shortcuts `fulfill`, `credit` and `transfer`. The adorm store and
  its generated models carry the column.

### Changed

- Store port (breaking for custom stores): `create_transaction` takes `metadata: dict | None = None`, which the engine
  passes on every call.

## 0.4.0 — 2026-11-23

Upgrade steps: [Upgrading to 0.4](https://docs.velis.si/allowances/guide/upgrading#upgrading-to-0-4).

### Added

- `allowances.sync`: every engine function of `allowances` as a plain function with the same signature, run to
  completion without an event loop, plus `unit_of_work(store)` as a synchronous context manager and the re-exported
  types, errors and definitions. `allowances.sync.driver.run()` and `synchronous()`. `StoreSuspendedError`, raised when
  a call through `allowances.sync` suspends.
- Django: `allowances_django.sync.shortcuts`, `allowances_django.sync.permissions` and
  `allowances_django.sync.store.DjangoStore`, the synchronous API over a store that runs the ORM on the calling thread.
- Django settings `ALLOWANCES_STORE` (default `"allowances_django.store.DjangoStore"`) and `ALLOWANCES_SYNC_STORE`
  (default `"allowances_django.sync.store.DjangoStore"`): the default stores of the asynchronous and the synchronous
  shortcuts, permissions and permission checks. `AllowancesBackend.get_sync_store()`.
- `allowances_adorm.store.AdormStore` (extra `allowances[adorm]`): the store port on adorm, over the
  `allowances_django` tables, on adorm's Django driver and on its native asyncpg driver. Generated adorm models in
  `allowances_adorm.models`.
- Guide page "Synchronous API" and backend page "adorm".
- Rights: a wallet whose effective rate for a feature is 0 and whose balance is positive covers the
  whole request at cost 0. `peek` checks it; `fulfill` records the use as a `consumption` entry of
  amount 0; `unfulfill` reverses it with `storno` entries of amount 0.
- `close_wallet()`: ends a wallet at a given instant without a ledger entry (revoking a right,
  closing a budget).
- `transfer()`: moves an amount between two wallets of the same currency as two `transfer` entries
  under one fulfillment record; `unfulfill` reverses both legs, and raises without writing anything
  when the target would end below zero or the source is archived. `TransactionKind.TRANSFER`.
- `Transaction.actor`: the subject on whose behalf an entry was made. `fulfill`, `credit`,
  `transfer` and `unfulfill` take `actor=`; `fulfill` defaults it to the only subject when
  `subjects` holds exactly one.
- Membership: `add_member()`, `remove_member()`, `groups_of()`. Groups are ordinary subjects and may
  be nested; a membership that would form a cycle raises `MembershipCycleError`. `peek` and `fulfill`
  expand every subject with its groups under the same key.
- Django: `allowances_django.permissions` with `AllowancesBackend` (an authentication backend that
  authenticates nobody and answers `has_perm` from grant wallets of the user and its groups), `PermissionProvider`,
  `grant_permission()`, `revoke_permission()` and `permission_feature()`.
- Django shortcuts `transfer`, `close_wallet`, `add_member`, `remove_member`, `groups_of`; `actor=` on
  `fulfill`, `unfulfill` and `credit`.
- Django: `Membership` model, `actor_type` / `actor_id` on `Transaction` and `TransactionArchive`,
  migration `0003_actor_transfer_membership`. The admin lists memberships and shows the actor of
  every transaction.
- Django: `AllowancesLock` model, migration `0004_allowanceslock`. `DjangoStore.lock_memberships`
  locks its row `"membership"` with `SELECT ... FOR UPDATE`, which serialises membership changes on
  every database with row locks; SQLite's `IMMEDIATE` transactions serialise them there. Migration
  0004 creates the row; a missing row is created on demand.
- Django setting `ALLOWANCES_ADMIN_READ_ONLY` (default `False`): when true, every allowances model is
  view-only in the admin.
- Store port methods `lock_wallets`, `add_membership`, `remove_membership`, `memberships_of`,
  `lock_memberships`; `create_transaction` takes `actor=`.
- `effective_allowances()` and `Allowance`: every live wallet of a subject and of its groups, of any
  currency and wallet type, with its holder, depth and effective balance. A snapshot: nothing is
  locked or written.
- Django: `shortcuts.effective_allowances()`, and `permissions.effective_permissions()`, which maps the
  Django permissions among a list of allowances to the allowances conferring them.
  `AllowancesBackend` computes its permission set with both.
- Guide pages "Rights and permissions", with a comparison with `django.contrib.auth`, and
  "Permissions quick start"; section "What a subject holds" in "Concepts".

### Changed

- The API is asynchronous (breaking): every engine function of `allowances`, every shortcut of
  `allowances_django.shortcuts` and `grant_permission` / `revoke_permission` are coroutine functions with unchanged
  names and signatures. Synchronous code imports them from `allowances.sync`, `allowances_django.sync.shortcuts` and
  `allowances_django.sync.permissions`.
- Store port (breaking for custom backends): every method is a coroutine function and `unit_of_work()` returns an
  asynchronous context manager. `MemoryStore` follows it and never suspends.
- Django: `allowances_django.store.DjangoStore` is the store of the asynchronous API. Outside a unit of work its calls
  run through `sync_to_async`; a unit of work runs on one thread of its own with its own connection. Without a running
  event loop it raises `StoreSuspendedError`. The ORM queries of both Django stores are in
  `allowances_django.queries.DjangoQueries`.
- `AllowancesBackend`: `has_perm`, `get_all_permissions` and `has_module_perms` use the synchronous API and
  `get_sync_store()`; `ahas_perm`, `aget_all_permissions` and `ahas_module_perms` use the asynchronous API and
  `get_store()`. Both share the per-user cache.
- Store port (breaking for custom backends): the five new methods above and the `actor` keyword of
  `create_transaction` are required. Wallet ids of one store must be mutually comparable.
- `find_unattached_fulfillment_records` and `Store.records_with_unattached_transactions` consider only
  `consumption` entries. A record holding only a `credit(guid=...)` without `obj`, or a transfer, is no
  longer reported.
- `fulfill`, `unfulfill`, `transfer`, `close_wallet`, `plan_switch`, `cancel_scheduled_switch` and
  `archive_expired_wallets` lock all the wallets they use in one `lock_wallets` call, in ascending id
  order, before reading balances. `fulfill`, `credit` and `transfer` given an existing `guid` lock its
  fulfillment record before any wallet, as `unfulfill` does.
- `consumed()` counts a positive storno only when it reverses a consumption.
- `plan_switch` and `cancel_scheduled_switch` switch a subject given under several keys once.
- `unfulfill` reads the record's transactions without row locks; the record lock serialises reversals.
- The Django shortcuts' default provider is `PermissionProvider(SettingsProvider())`: features and
  currencies named `perm:<app_label>.<codename>` are served without configuration.

### Fixed

- A grant wallet with a conversion rate of 0 never satisfied `peek` or `fulfill`.
- Where several modifiers apply, the smallest factor is used, the one most favourable to the
  subject; the largest was used.
- A wallet whose `valid_until` equals `at` was both valid and expired at `at`. Validity is now
  `valid_from <= at < valid_until`: at its `valid_until` a wallet no longer funds requests, so a wallet
  closed at an instant and one opened at it (`close_wallet`, `plan_switch`) are never valid together.
  Modifiers use the same interval.
- Concurrent `fulfill` calls whose fulfillment orders walk the same wallets in different orders, and
  `fulfill` against `unfulfill`, could deadlock on PostgreSQL: wallets were locked step by step in walk
  or transaction order.
- A nested `MemoryStore.unit_of_work()` block that raised kept its writes when the outer block went on:
  an operation refused inside a caller's block (e.g. `transfer` raising `InsufficientBalanceError`)
  left an empty fulfillment record behind. A nested block now discards its own writes, as a nested
  `atomic()` does; the Store port requires this of every backend.

## 0.3.0 — 2026-11-05

Upgrade steps: [Upgrading to 0.3](https://docs.velis.si/allowances/guide/upgrading).

### Added

- `credit()`: adds to a wallet's balance as a `grant` ledger entry, attached to the paying object;
  with a `guid` it is reversible by `unfulfill`.
- `open_wallet()`: opens one wallet with a `source`. Wallets whose source is not `"plan"` (purchased
  credits, budgets) are never closed or adjusted by `plan_switch`.
- `WalletSpec.on_switch` with `SwitchPolicy.REPLACE` (default), `ADJUST` and `TOP_UP`: a plan switch
  can keep a plan wallet and move its balance by the difference between the old and the new
  allotment.
- `adjust_wallet()`: changes the allotment of a wallet opened with `ADJUST` or `TOP_UP`.
- `consumed()`: net consumption of a wallet in a period, for billing postpaid budgets.
- `fulfillment_order` steps take an optional third element, `source`.
- Scheduled plan switches (future `effective_date`) support every switch policy, are cancelled by
  `cancel_scheduled_switch()` until they take effect, and are replaced by the next switch for the same
  subject.
- `Wallet.source`, `Wallet.allotment` and `Wallet.scheduled`; Django migration
  `0002_wallet_source_allotment_scheduled`.
- `WalletNotFoundError`, `UnsuitableWalletError`.
- Guide page "Credits and budgets" with recipes for prepaid credits, monthly plan credits and
  postpaid budgets.

### Changed

- `plan_switch` closes only wallets with `source == "plan"`. Existing wallets default to `"plan"`.
- Within a `fulfillment_order` step, wallets are drawn soonest `valid_until` first, wallets without
  one last; equal expiries keep oldest-first order.
- Wallets with an `allotment` are not archived when drained.
- `credit()` refuses a wallet that is not yet valid.
- Store port (breaking for custom backends): `create_wallet` takes `source`, `allotment` and
  `scheduled`; `save_wallet` also persists `overdraft_limit`, `reset_config`, `allotment` and
  `scheduled`; `find_wallets` accepts `currencies=None`; `close_wallets` is removed; new methods
  `find_scheduled_wallets` and `transactions_for_wallet`.

### Fixed

- `allowances.__version__` matches the package version.
