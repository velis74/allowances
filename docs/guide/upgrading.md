# Upgrading to 0.3

## Django: run the migration

```sh
python manage.py migrate allowances
```

`0002_wallet_source_and_allotment` adds `source` and `allotment` to `Wallet` and `WalletArchive`. Every existing row gets `source="plan"` and `allotment=NULL`, which keeps `plan_switch` closing and opening them exactly as before.

## Wallets you opened outside a plan

`plan_switch` now closes only wallets with `source="plan"`. A wallet your application opened directly through `store.create_wallet` — a purchased pack, say — is `"plan"` after the migration and is still closed by the next switch. To keep it out of plan switches, give it its own source in a data migration:

```python
def mark_purchases(apps, schema_editor):
    Wallet = apps.get_model("allowances", "Wallet")
    Wallet.objects.filter(pk__in=purchased_wallet_ids()).update(source="purchase")
```

New wallets of that kind are opened with `open_wallet(..., source="purchase")` and funded with `credit`.

## Plan wallets and switch policies

Specs default to `SwitchPolicy.REPLACE`, so existing `plan_switch` calls behave as before. A spec with `ADJUST` or `TOP_UP` keeps a current plan wallet only when that wallet has an `allotment`; wallets opened before the upgrade have none and are replaced on the first such switch, without carrying their balance. To carry balances from the first switch, set the allotment of the existing wallets to the amount their plan gave:

```python
Wallet.objects.filter(source="plan", currency="credits", valid_until__isnull=True).update(allotment=1000)
```

Wallets with an allotment are no longer archived when drained; see [Switch policies](/guide/plans-and-archival#switch-policies).

A switch with an `ADJUST` or `TOP_UP` spec cannot take a future `effective_date`.

## Draw order within a step

Within a `fulfillment_order` step, wallets were drawn oldest first. They are now drawn soonest `valid_until` first, wallets without one last, equal expiries oldest first. A subject whose wallets in one step have different expiries may be charged from a different wallet than before. To keep oldest-first order, use a callable `fulfillment_order`:

```python
def oldest_first(wallets):
    return sorted(wallets, key=lambda w: (w.created_at, w.id))
```

## Ledger reports

`TransactionKind.GRANT` entries are now written: by `credit` (positive) and by a plan switch or `adjust_wallet` that changes a stored balance (either sign). A report that sums every transaction of a wallet as consumption needs to filter by `kind`, or use `consumed()`.

## Custom backends

The store port changes:

- `create_wallet` takes `source` (default `"plan"`) and `allotment` (default `None`), and the returned `Wallet` carries both.
- `save_wallet` also persists `overdraft_limit`, `reset_config` and `allotment`.
- `close_wallets` takes `source` (`None` = every source) and `exclude` (ids that stay open).
- New method `transactions_for_wallet(*, wallet_id, since, until)`, ordered by `(created_at, id)`.

An archive should keep `source` and `allotment` along with the other wallet fields. See [Writing a backend](/backends/writing-a-backend).
