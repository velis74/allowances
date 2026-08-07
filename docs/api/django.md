# Django adapter API

Everything in `allowances_django`. Requires `pip install "allowances[django]"` and `"allowances_django"` in `INSTALLED_APPS`.

::: tip Import from submodules
`allowances_django/__init__.py` is deliberately empty. Django imports an application package while building the app registry, before models are ready, so anything importing models from there would break startup. Use `from allowances_django.shortcuts import fulfill`.
:::

## Shortcuts

`allowances_django.shortcuts` — the same functions as the [core engine](/api/core#engine), minus the ceremony. The store and provider are supplied for you, and subjects are the model instances you already have.

```python
from allowances_django.shortcuts import (
    peek, fulfill, attach, unfulfill, plan_switch,
    archive_expired_wallets, find_unattached_fulfillment_records,
)
```

```python
peek(feature, quantity, subjects, *, provider=None, at=None, store=None)
fulfill(feature, quantity, subjects, *, guid=None, allow_partial=False,
        provider=None, at=None, store=None)
attach(guid, obj, *, store=None)
unfulfill(guid, *, at=None, store=None)
plan_switch(subjects, open_wallets, effective_date=None, *, store=None)
archive_expired_wallets(at=None, *, store=None)
find_unattached_fulfillment_records(older_than=None, *, store=None)
```

`subjects` and `obj` take model instances or refs, interchangeably. `store=` targets another database; `provider=` swaps the definition source — per-tenant configuration assembled at request time, say.

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

## Models

`allowances_django.models`. Tables are prefixed `allowances_`; the app label is `allowances`.

These are a persistence detail, not the library's vocabulary — the engine never imports them. Query them for reporting and admin; call the shortcuts to change anything.

### `Wallet`

`subject_type`, `subject_id`, `wallet_type`, `currency`, `balance`, `valid_from`, `valid_until`, `overdraft_limit`, `first_overdraft_at`, `reset_config`, `created_at`, `updated_at`.

Amounts are `DecimalField(max_digits=20, decimal_places=6)`.

### `Transaction`

`wallet` (FK, cascade), `record` (FK, null), `kind`, `amount`, `feature`, `storno_of` (self FK, protected), `object_type`, `object_id`, `created_at`.

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

### `WalletArchive` / `TransactionArchive`

Where spent-out and expired wallets go. No foreign key back to the live wallet — it's gone by then; `original_wallet_id` is a plain reference. `TransactionArchive.storno_of` points at archived copies, rebuilt during archival.

## Settings

```python
ALLOWANCES = {
    "currencies": {...},
    "features": {...},
}
```

The full shape is in [Concepts](/guide/concepts). Absent, `SettingsProvider` reads an empty config and every lookup raises `DefinitionNotFoundError`.

## Admin

All six models registered. Wallets and modifiers editable; transactions and archives read-only, [on purpose](/backends/django#admin).
