# Django adapter API

Everything in `allowances_django`. Requires `pip install "allowances[django]"` and `"allowances_django"` in
`INSTALLED_APPS`.

::: tip Import from submodules
`allowances_django/__init__.py` is empty. Django imports an application package while building the app registry,
before models are ready, so a model import there would break startup. Use
`from allowances_django.shortcuts import fulfill`.
:::

## Shortcuts

`allowances_django.shortcuts`: the [core engine](/api/core#engine) functions with the store and provider supplied, as
coroutine functions. Subjects may be model instances. [`allowances_django.sync.shortcuts`](#synchronous-modules) has
the same functions as plain functions.

```python
from allowances_django.shortcuts import (
    peek, fulfill, attach, unfulfill, plan_switch, cancel_scheduled_switch,
    open_wallet, credit, transfer, adjust_wallet, close_wallet, consumed,
    archive_expired_wallets, find_unattached_fulfillment_records, transactions_for_object,
    add_member, remove_member, groups_of, effective_allowances,
)
```

```python
async def peek(feature, quantity, subjects, *, provider=None, at=None, store=None)
async def fulfill(feature, quantity, subjects, *, guid=None, allow_partial=False,
                  actor=None, metadata=None, provider=None, at=None, store=None)
async def attach(guid, obj, *, store=None)
async def unfulfill(guid, *, at=None, actor=None, store=None)
async def plan_switch(subjects, open_wallets, effective_date=None, *, store=None)
async def cancel_scheduled_switch(subjects, *, store=None)
async def open_wallet(subject, spec, *, source, effective_date=None, store=None)
async def credit(wallet, amount, *, guid=None, obj=None, actor=None, metadata=None, at=None,
                 store=None)
async def transfer(from_wallet, to_wallet, amount, *, guid=None, obj=None, actor=None,
                   metadata=None, at=None, store=None)
async def adjust_wallet(wallet, allotment, *, at=None, store=None)
async def close_wallet(wallet, *, at=None, store=None)
async def consumed(wallet, since, until, *, store=None)
async def archive_expired_wallets(at=None, *, store=None)
async def find_unattached_fulfillment_records(older_than=None, *, store=None)
async def transactions_for_object(obj, *, store=None) -> list[Transaction]
async def add_member(member, group, *, store=None)
async def remove_member(member, group, *, store=None) -> bool
async def groups_of(obj, *, store=None) -> list
async def effective_allowances(subject, *, at=None, wallet_types=None, currencies=None,
                               store=None) -> list[Allowance]
```

`subjects`, `subject`, `member`, `group`, `actor` and `obj` take model instances or refs, interchangeably. `wallet`,
`from_wallet` and `to_wallet` take a `Wallet` value, a `Wallet` row or its id. `store=` replaces the default store,
`settings.ALLOWANCES_STORE` (see [Settings](#settings)), e.g. for another database; `provider=` replaces the
definition source, e.g. per-tenant configuration assembled per request. The default provider
is `PermissionProvider(SettingsProvider())`: `settings.ALLOWANCES` plus the [`perm:` features](#permissions).

`groups_of` returns the groups as model instances, nearest first; a group whose reference resolves to no existing
object is left out.

`transactions_for_object` is [`allowances.transactions_for_object`](/api/core#transactions-for-object) with the store
supplied; `obj` is a model instance or an `ObjectRef`, converted as by `attach`.

`effective_allowances` is [`allowances.effective_allowances`](/api/core#effective-allowances) with the store supplied:
every live wallet of `subject` (a model instance or a `SubjectRef`) and of its groups, as `Allowance` values whose
`holder` is a `SubjectRef`. Wallets of every balance are listed. [`effective_permissions`](#permissions) turns the
result into Django permissions.

```python
result = await fulfill("send_email", 1, {"user": user, "team": team})
await attach(result.guid, message)
```

```python
get_store(store=None) -> Store
get_provider(provider=None) -> DefinitionProvider
```

`get_store` returns `store`, or a new store from `settings.ALLOWANCES_STORE`; `get_provider` returns `provider`, or the
default provider.

## Subjects

`allowances_django.subjects`

```python
subject_ref(obj) -> SubjectRef      # instance or ref -> ref
object_ref(obj) -> ObjectRef        # same, for attached objects
subject_refs(subjects: dict) -> dict  # a whole mapping at once
resolve(ref) -> Model | None        # ref -> instance, or None if gone
```

`subject_ref` produces `SubjectRef("auth.user", "42")` from a `User`. It raises `ValueError` for an unsaved instance
and `TypeError` for an object that is not a model instance.

`resolve` returns `None` instead of raising when the object has been deleted or its model does not exist; the
references are not enforced by the database. It queries the database synchronously; asynchronous code calls
`await store.resolve(ref)` on a Django store.

## Stores

Two stores over the ORM with the same queries (`allowances_django.queries.DjangoQueries`) and the same constructor;
[Two stores](/backends/django#two-stores) describes their execution.

```python
allowances_django.store.DjangoStore(using: str | None = None)        # the asynchronous API
allowances_django.sync.store.DjangoStore(using: str | None = None)   # the synchronous API, never suspends
```

A [Store](/backends/). `using` routes every query to that database alias (`None`: the default database); the `using`
property returns it. A store holds no connection, only a cache mapping fulfilment record guids to primary keys.

The asynchronous store runs a call outside a unit of work through `sync_to_async`, and everything inside
`async with store.unit_of_work():` on one thread of that unit of work, where `atomic()` is entered and exited and whose
connections are closed when the block ends. Called without a running event loop it raises `StoreSuspendedError`. The
synchronous store runs every query on the calling thread; its `unit_of_work()` is `transaction.atomic()`.

Besides the port, both have two coroutine methods the shortcuts use:

```python
async def resolve(ref) -> Model | None
async def live_wallet_ids(*, subject, wallet_type, currency, at) -> list
```

`resolve` is [`subjects.resolve`](#subjects). `live_wallet_ids` returns, ascending, the ids of the live wallets of
`subject` with that type and currency whose `valid_until` is empty or after `at`, wallets not yet valid included.

`lock_wallets` is `select_for_update()` ordered by `id` where the database has it. `lock_memberships` locks the
`AllowancesLock` row `MEMBERSHIP_LOCK_NAME` (`"membership"`) with `select_for_update()`. A missing row is created in
the current transaction; when a concurrent transaction created it and the isolation level hides it (REPEATABLE READ,
SERIALIZABLE), `lock_memberships` raises `django.db.OperationalError` and a retry succeeds. See
[Concurrency](/backends/django#concurrency).

Row-to-value converters, for reports written against the models (`allowances_django.store`, also
`allowances_django.queries`):

```python
to_wallet(row) -> Wallet
to_transaction(row, record_guid=None) -> Transaction
to_record(row) -> FulfillmentRecord
```

## Providers

`allowances_django.providers`

```python
SettingsProvider(config: dict | None = None)
```

Reads `settings.ALLOWANCES`, or the `config` dict if given, in which case settings are not read (e.g. in tests or
per-tenant setups).

## Permissions

`allowances_django.permissions` — Django permissions held as grant wallets. See [Rights and
permissions](/guide/permissions) and the [Permissions quick start](/guide/permissions-quick-start).

```python
PERMISSION_PREFIX = "perm:"
PERMISSION_SOURCE = "permission"
permission_feature(perm) -> str
PermissionProvider(base: DefinitionProvider | None = None)
async def grant_permission(subject, perm, *, valid_from=None, valid_until=None, store=None) -> Wallet
async def revoke_permission(subject, perm, *, at=None, store=None) -> int
effective_permissions(allowances: Iterable[Allowance]) -> dict[str, list[Allowance]]
AllowancesBackend
```

`permission_feature("club.change_member")` is `"perm:club.change_member"`, the name of both the feature and the currency
of that permission.

`PermissionProvider` delegates to `base` (default `SettingsProvider()`). For a feature or currency named with the prefix
that `base` does not define, it serves one: the currency without conversion and with default rounding; the feature with
a grant section accepting its own currency at rate 0, no subjects restriction, and the fulfilment order `[("grant",
None)]`, so a wallet of any supplied subject or of its groups holds the right.

`grant_permission` opens a `GRANT` wallet for `subject` in currency `perm:<perm>` with balance 1 and source
`"permission"` (no `plan_switch` touches it), valid from `valid_from` (default now) until `valid_until` (default: no
end).

`revoke_permission` calls `close_wallet` at `at` (default now) on every live grant wallet of `subject` in that currency
that has not ended by then, including wallets that start later, in one unit of work. Returns how many it closed.

`effective_permissions` filters a list of allowances down to Django permissions. It keeps the `GRANT` allowances whose
currency starts with `PERMISSION_PREFIX` and whose `balance` is positive, and maps each permission
`"app_label.codename"` (the currency without the prefix) to the allowances conferring it, in input order. Keys are
sorted. It reads nothing from the database; pass it the output of `effective_allowances`:

```python
effective_permissions(await effective_allowances(user))
# {"club.change_member": [Allowance(wallet=Wallet(id=7, ...), holder=SubjectRef("auth.group", "1"),
#                                   depth=1, balance=Decimal("1.000000"))]}
```

It applies no superuser or `is_active` rule; those belong to `AllowancesBackend`.

`AllowancesBackend` is an authentication backend that authenticates nobody (`authenticate`, `get_user` return `None`)
and answers permission checks:

| | |
| --- | --- |
| `get_all_permissions(user_obj, obj=None)` | `set()` for an inactive or anonymous user and for any `obj`. Every `Permission` as `"app_label.codename"` for an active superuser. Otherwise `set(effective_permissions(effective_allowances(user_obj, wallet_types=[WalletType.GRANT])))`: the permissions held by grant wallets of the user and of every group in `groups_of(user)` that are valid now and have a positive effective balance. Cached on the user object (`_allowances_perm_cache`). |
| `has_perm(user_obj, perm, obj=None)` | `user_obj.is_active` and `perm` in `get_all_permissions(user_obj, obj)`. |
| `has_module_perms(user_obj, app_label)` | Whether the user is active and holds any permission starting with `app_label + "."`. |

`has_perm`, `get_all_permissions` and `has_module_perms` run the synchronous API (`allowances.sync`) with the store
from `get_sync_store()`, by default one from `settings.ALLOWANCES_SYNC_STORE`.

`aget_all_permissions`, `ahas_perm`, `ahas_module_perms`, `aauthenticate` and `aget_user` are the asynchronous variants,
under the names Django 5.2 and later calls from asynchronous code (`await user.ahas_perm(...)`). They run the
asynchronous API with the store from `get_store()`, by default one from `settings.ALLOWANCES_STORE`. The synchronous and
the asynchronous checks share the per-user cache.

Override `get_store()` and `get_sync_store()` in a subclass to read another database or use another store; the store
of `get_sync_store()` must never suspend.

## Synchronous modules

`allowances_django.sync.shortcuts` has every shortcut above as a plain function with the same signature, plus
`get_store(store=None)` (`store`, or a new store from `settings.ALLOWANCES_SYNC_STORE`) and `get_provider`. Each call
passes `store=`, or the store from `ALLOWANCES_SYNC_STORE`, to the coroutine and runs it with
`allowances.sync.driver.run`; the store must never suspend.

`allowances_django.sync.permissions` has `grant_permission` and `revoke_permission` as plain functions, run the same
way, and re-exports `AllowancesBackend`, `PermissionProvider`, `effective_permissions`, `permission_feature`,
`PERMISSION_PREFIX` and `PERMISSION_SOURCE`.

`allowances_django.sync.store.DjangoStore` is described under [Stores](#stores). See [Synchronous
API](/guide/synchronous#django).

## Models

`allowances_django.models`. Tables are prefixed `allowances_`; the app label is `allowances`.

The engine does not import the models. Query them for reporting and admin; change data through the shortcuts.

### `Wallet`

`subject_type`, `subject_id`, `wallet_type`, `currency`, `balance`, `valid_from`, `valid_until`, `overdraft_limit`,
`first_overdraft_at`, `reset_config`, `source`, `allotment`, `scheduled`, `created_at`, `updated_at`.

Amounts are `DecimalField(max_digits=20, decimal_places=6)`.

### `Transaction`

`wallet` (FK, cascade), `record` (FK, null), `kind`, `amount`, `feature`, `storno_of` (self FK, protected),
`object_type`, `object_id`, `actor_type`, `actor_id`, `metadata`, `created_at`.

`actor_type` / `actor_id` name the subject on whose behalf the entry was made; both are empty when it has none.
`metadata` (`JSONField`, null) holds the application's data given to `fulfill`, `credit` or `transfer`, copied onto
stornos; `NULL` when there is none.

A partial unique constraint on `storno_of` enforces that a transaction is reversed at most once.

### `FulfillmentRecord`

`guid` (unique), `created_at`, `updated_at`.

### `LicenseModifier`

`subject_type`, `subject_id`, `feature`, `factor`, `valid_from` (inclusive), `valid_until` (exclusive).

The engine only reads modifiers; the application creates them:

```python
await LicenseModifier.objects.acreate(
    subject_type="auth.user", subject_id=str(user.pk),
    feature="send_email", factor=Decimal("0.5"),
    valid_until=end_of_promo,
)
```

### `Membership`

`member_type`, `member_id`, `group_type`, `group_id`, `created_at`.

One subject's direct membership of a group. Unique on the four subject columns, indexed by member and by group. Change
memberships with `add_member` and `remove_member`, which reject cycles.

### `WalletArchive` / `TransactionArchive`

Spent-out and expired wallets and their transactions. Archival deletes the live wallet, so there is no foreign key to
it; `original_wallet_id` is a plain reference. `TransactionArchive.storno_of` points at archived copies, rebuilt during
archival. `TransactionArchive` keeps `actor_type`, `actor_id` and `metadata`.

### `AllowancesLock`

`name` (primary key). A row that the stores lock with `select_for_update()` to serialise one kind of change; the row
`"membership"` serialises membership changes. The store creates a missing row. It holds no data.

## Settings

```python
ALLOWANCES = {
    "currencies": {...},
    "features": {...},
}
```

The full shape is in [Concepts](/guide/concepts). Absent, `SettingsProvider` reads an empty config and every lookup
raises `DefinitionNotFoundError`.

```python
ALLOWANCES_STORE = "allowances_django.store.DjangoStore"
ALLOWANCES_SYNC_STORE = "allowances_django.sync.store.DjangoStore"
```

The default stores: `ALLOWANCES_STORE` of `allowances_django.shortcuts`, `allowances_django.permissions` and the
asynchronous checks of `AllowancesBackend`; `ALLOWANCES_SYNC_STORE` of `allowances_django.sync.shortcuts`,
`allowances_django.sync.permissions` and the synchronous checks. Each is a dotted path to a callable or a callable,
called with no arguments for every call without `store=`. The values above are the defaults.

```python
ALLOWANCES_ADMIN_READ_ONLY = False
```

`True` makes every allowances model view-only in the [admin](#admin).

## adorm store

`allowances_adorm.store`, with `pip install "allowances[adorm]"`; see [adorm backend](/backends/adorm).

```python
AdormStore(using: str | None = None, *, session_name: str = "default")
```

A [Store](/backends/) over adorm models generated from `allowances_django.models` (`allowances_adorm.models`). With
`using` (an adorm profile name) every call runs on the adorm session `(using, session_name)`; without it, on
`adorm.current_session()`, so the store joins the caller's `adorm.atomic()` block. `unit_of_work()` is
`session.atomic()`. The methods suspend: the store serves only the asynchronous API. `session()` returns the session the
next call runs on.

## Admin

The seven ledger models are registered. Wallets, fulfilment records and modifiers are editable; transactions,
memberships and archives cannot be added or changed, only deleted ([why](/backends/django#admin)). The transaction list
shows and searches the actor.

With `ALLOWANCES_ADMIN_READ_ONLY = True` every allowances `ModelAdmin` returns `False` from `has_add_permission`,
`has_change_permission` and `has_delete_permission`; `has_view_permission` follows Django's view permission. The setting
is read on every call, so `override_settings` applies. The classes derive from
`allowances_django.admin.AllowancesModelAdmin`.
