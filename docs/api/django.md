# Django adapter API

Everything in `allowances_django`. Requires `pip install "allowances[django]"` and `"allowances_django"` in `INSTALLED_APPS`.

::: tip Import from submodules
`allowances_django/__init__.py` is deliberately empty. Django imports an application package while building the app registry, before models are ready, so anything importing models from there would break startup. Use `from allowances_django.shortcuts import fulfill`.
:::

## Shortcuts

`allowances_django.shortcuts` — the same functions as the [core engine](/api/core#engine), minus the ceremony. The store and provider are supplied for you, and subjects are the model instances you already have.

```python
from allowances_django.shortcuts import (
    peek, fulfill, attach, unfulfill, plan_switch, cancel_scheduled_switch,
    open_wallet, credit, transfer, adjust_wallet, close_wallet, consumed,
    archive_expired_wallets, find_unattached_fulfillment_records,
    add_member, remove_member, groups_of, effective_allowances,
)
```

```python
peek(feature, quantity, subjects, *, provider=None, at=None, store=None)
fulfill(feature, quantity, subjects, *, guid=None, allow_partial=False,
        actor=None, provider=None, at=None, store=None)
attach(guid, obj, *, store=None)
unfulfill(guid, *, at=None, actor=None, store=None)
plan_switch(subjects, open_wallets, effective_date=None, *, store=None)
cancel_scheduled_switch(subjects, *, store=None)
open_wallet(subject, spec, *, source, effective_date=None, store=None)
credit(wallet, amount, *, guid=None, obj=None, actor=None, at=None, store=None)
transfer(from_wallet, to_wallet, amount, *, guid=None, obj=None, actor=None,
         at=None, store=None)
adjust_wallet(wallet, allotment, *, at=None, store=None)
close_wallet(wallet, *, at=None, store=None)
consumed(wallet, since, until, *, store=None)
archive_expired_wallets(at=None, *, store=None)
find_unattached_fulfillment_records(older_than=None, *, store=None)
add_member(member, group, *, store=None)
remove_member(member, group, *, store=None) -> bool
groups_of(obj, *, store=None) -> list
effective_allowances(subject, *, at=None, wallet_types=None, currencies=None,
                     store=None) -> list[Allowance]
```

`subjects`, `subject`, `member`, `group`, `actor` and `obj` take model instances or refs, interchangeably. `wallet`, `from_wallet` and `to_wallet` take a `Wallet` value, a `Wallet` row or its id. `store=` targets another database; `provider=` swaps the definition source — per-tenant configuration assembled at request time, say. The default provider is `PermissionProvider(SettingsProvider())`: `settings.ALLOWANCES` plus the [`perm:` features](#permissions).

`groups_of` returns the groups as model instances, nearest first; a group whose reference resolves to no existing object is left out.

`effective_allowances` is [`allowances.effective_allowances`](/api/core#effective-allowances) with the store supplied: every live wallet of `subject` (a model instance or a `SubjectRef`) and of its groups, as `Allowance` values whose `holder` is a `SubjectRef`. Wallets of every balance are listed. [`effective_permissions`](#permissions) turns the result into Django permissions.

```python
fulfill("send_email", 1, {"user": request.user, "team": team})
attach(result.guid, message)
```

## Subjects

`allowances_django.subjects`

```python
subject_ref(obj) -> SubjectRef      # instance or ref -> ref
object_ref(obj) -> ObjectRef        # same, for attached objects
subject_refs(subjects: dict) -> dict  # a whole mapping at once
resolve(ref) -> Model | None        # ref -> instance, or None if gone
```

`subject_ref` produces `SubjectRef("auth.user", "42")` from a `User`. It raises `ValueError` on an unsaved instance and `TypeError` on something that isn't a model.

`resolve` returns `None` rather than raising when the subject has been deleted or its model no longer exists — these references aren't database-enforced, so an admin page should be able to render an orphaned row.

## Store

`allowances_django.store`

```python
DjangoStore(using: str | None = None)
```

A [Store](/backends/) over the ORM. `using` routes every query to that database alias. Cheap to construct — it holds no connection, only a small cache mapping record guids to primary keys.

`lock_wallets` is `select_for_update()` ordered by `id` where the database has it. `lock_memberships` takes the PostgreSQL transaction-level advisory lock `MEMBERSHIP_LOCK_KEY` (`allowances_django.store.MEMBERSHIP_LOCK_KEY`) and does nothing on other databases. See [Concurrency](/backends/django#concurrency).

Row-to-value converters, useful when writing reports against the models directly:

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

Reads `settings.ALLOWANCES`, or the `config` dict if given — which bypasses settings entirely and is what tests and per-tenant setups want.

## Permissions

`allowances_django.permissions` — Django permissions held as grant wallets. See [Rights and permissions](/guide/permissions) and the [Permissions quick start](/guide/permissions-quick-start).

```python
PERMISSION_PREFIX = "perm:"
PERMISSION_SOURCE = "permission"
permission_feature(perm) -> str
PermissionProvider(base: DefinitionProvider | None = None)
grant_permission(subject, perm, *, valid_from=None, valid_until=None, store=None) -> Wallet
revoke_permission(subject, perm, *, at=None, store=None) -> int
effective_permissions(allowances: Iterable[Allowance]) -> dict[str, list[Allowance]]
AllowancesBackend
```

`permission_feature("club.change_member")` is `"perm:club.change_member"`, the name of both the feature and the currency of that permission.

`PermissionProvider` delegates to `base` (default `SettingsProvider()`). For a feature or currency named with the prefix that `base` does not define, it serves one: the currency without conversion and with default rounding; the feature with a grant section accepting its own currency at rate 0, no subjects restriction, and the fulfillment order `[("grant", None)]`, so a wallet of any supplied subject or of its groups holds the right.

`grant_permission` opens a `GRANT` wallet for `subject` in currency `perm:<perm>` with balance 1 and source `"permission"` (no `plan_switch` touches it), valid from `valid_from` (default now) until `valid_until` (default: no end).

`revoke_permission` calls `close_wallet` at `at` (default now) on every live grant wallet of `subject` in that currency that has not ended by then, including wallets that start later, in one unit of work. Returns how many it closed.

`effective_permissions` filters a list of allowances down to Django permissions. It keeps the `GRANT` allowances whose currency starts with `PERMISSION_PREFIX` and whose `balance` is positive, and maps each permission `"app_label.codename"` (the currency without the prefix) to the allowances conferring it, in input order. Keys are sorted. It reads nothing from the database; pass it the output of `effective_allowances`:

```python
effective_permissions(effective_allowances(user))
# {"club.change_member": [Allowance(wallet=Wallet(id=7, ...), holder=SubjectRef("auth.group", "1"),
#                                   depth=1, balance=Decimal("1.000000"))]}
```

It applies no superuser or `is_active` rule; those belong to `AllowancesBackend`.

`AllowancesBackend` is an authentication backend that authenticates nobody (`authenticate`, `get_user` return `None`) and answers permission checks:

| | |
| --- | --- |
| `get_all_permissions(user_obj, obj=None)` | `set()` for an inactive or anonymous user and for any `obj`. Every `Permission` as `"app_label.codename"` for an active superuser. Otherwise `set(effective_permissions(effective_allowances(user_obj, wallet_types=[WalletType.GRANT])))`: the permissions held by grant wallets of the user and of every group in `groups_of(user)` that are valid now and have a positive effective balance. Cached on the user object (`_allowances_perm_cache`). |
| `has_perm(user_obj, perm, obj=None)` | `user_obj.is_active` and `perm` in `get_all_permissions(user_obj, obj)`. |
| `has_module_perms(user_obj, app_label)` | Whether the user is active and holds any permission starting with `app_label + "."`. |

`aget_all_permissions`, `ahas_perm`, `ahas_module_perms`, `aauthenticate` and `aget_user` are the async variants. `get_store()` returns the `DjangoStore` it reads; override it in a subclass to read another database.

## Models

`allowances_django.models`. Tables are prefixed `allowances_`; the app label is `allowances`.

These are a persistence detail, not the library's vocabulary — the engine never imports them. Query them for reporting and admin; call the shortcuts to change anything.

### `Wallet`

`subject_type`, `subject_id`, `wallet_type`, `currency`, `balance`, `valid_from`, `valid_until`, `overdraft_limit`, `first_overdraft_at`, `reset_config`, `source`, `allotment`, `scheduled`, `created_at`, `updated_at`.

Amounts are `DecimalField(max_digits=20, decimal_places=6)`.

### `Transaction`

`wallet` (FK, cascade), `record` (FK, null), `kind`, `amount`, `feature`, `storno_of` (self FK, protected), `object_type`, `object_id`, `actor_type`, `actor_id`, `created_at`.

`actor_type` / `actor_id` name the subject on whose behalf the entry was made; both are empty when it has none.

A partial unique constraint on `storno_of` enforces that a transaction is reversed at most once.

### `FulfillmentRecord`

`guid` (unique), `created_at`, `updated_at`.

### `LicenseModifier`

`subject_type`, `subject_id`, `feature`, `factor`, `valid_from`, `valid_until`.

Create these yourself — the engine only reads them:

```python
LicenseModifier.objects.create(
    subject_type="auth.user", subject_id=str(user.pk),
    feature="send_email", factor=Decimal("0.5"),
    valid_until=end_of_promo,
)
```

### `Membership`

`member_type`, `member_id`, `group_type`, `group_id`, `created_at`.

One subject's direct membership of a group. Unique on the four subject columns, indexed by member and by group. Change memberships with `add_member` and `remove_member`, which reject cycles.

### `WalletArchive` / `TransactionArchive`

Where spent-out and expired wallets go. No foreign key back to the live wallet — it's gone by then; `original_wallet_id` is a plain reference. `TransactionArchive.storno_of` points at archived copies, rebuilt during archival. `TransactionArchive` keeps `actor_type` and `actor_id`.

## Settings

```python
ALLOWANCES = {
    "currencies": {...},
    "features": {...},
}
```

The full shape is in [Concepts](/guide/concepts). Absent, `SettingsProvider` reads an empty config and every lookup raises `DefinitionNotFoundError`.

## Admin

All seven models registered. Wallets and modifiers editable; transactions, memberships and archives read-only, [on purpose](/backends/django#admin). The transaction list shows and searches the actor.
