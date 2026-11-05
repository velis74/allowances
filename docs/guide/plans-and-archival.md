# Plans and archival

## Plan switch

Putting a subject on a different plan means two things happening together: what the old plan gave stops, and what the new one gives starts. If those come apart — even for a moment — the subject either has both plans or neither.

```python
from allowances_django.shortcuts import plan_switch

plan_switch(
    {"user": user},
    [
        WalletSpec(WalletType.CONSUMABLE, "email_send", Decimal("2000")),
        WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("500")),
        WalletSpec(WalletType.GRANT, "api_access", Decimal("1")),
    ],
)
```

Every plan wallet the subject currently holds gets `valid_until` set, and the new set is opened with a matching `valid_from`. One transaction, all or nothing.

**Only plan wallets take part.** `plan_switch` opens wallets with `source="plan"` and closes only those. A wallet opened with [`open_wallet`](/guide/credits-and-budgets) under another source — a purchased credit pack, a spending budget — keeps its balance and validity through every plan change.

**Every spec is opened once per subject.** Passing two subjects and three specs opens six wallets — each subject gets their own. That is usually what a shared plan means.

**Already-closed wallets keep their original end date.** A wallet that expired last week is not re-closed with today's timestamp; its history stays accurate.

**Scheduling is just a future date:**

```python
plan_switch({"user": user}, specs, effective_date=next_billing_date)
```

The old wallets stay valid until then and the new ones aren't visible before it. Nothing needs to run on the day. This holds for specs with the default `replace` policy; a switch that keeps wallets cannot be scheduled (see below).

## Switch policies

What happens to an existing plan wallet depends on what it holds, so each `WalletSpec` says it with `on_switch`:

| Policy | The subject's current plan wallet of the same type and currency | Fits |
| --- | --- | --- |
| `SwitchPolicy.REPLACE` (default) | Closed with whatever was left in it; a new wallet opens with the spec's balance. A later reversal of earlier consumption goes back to the closed wallet. | Monthly quotas that start over with the new plan; boolean grants. |
| `SwitchPolicy.ADJUST` | Kept; its balance moves by *new allotment − old allotment*, which may be negative. | Capacities — storage, seats — where usage must survive the switch; rate quotas. |
| `SwitchPolicy.TOP_UP` | Kept; it receives only a positive difference. | Plan credits: an upgrade adds what the bigger plan gives on top, a downgrade takes nothing away. |

```python
from allowances import SwitchPolicy

plan_switch({"user": user}, [
    WalletSpec(WalletType.CONSUMABLE, "storage_gb", Decimal("100"), on_switch=SwitchPolicy.ADJUST),
    WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("3000"), on_switch=SwitchPolicy.TOP_UP),
    WalletSpec(WalletType.GRANT, "api_access", Decimal("1")),
])
```

A user on 10 GB with 8 GB in use who moves to 100 GB has 92 GB left; a reversal of an upload made before the switch (deleting the file) frees space in the same wallet. A user with 300 of 1000 plan credits left who moves to the 3000 plan has 2300.

The arithmetic needs the old plan's amount, so a wallet opened under `adjust` or `top_up` records it as `allotment`:

- `adjust` sets the allotment to the new amount. On a downgrade below current usage the balance goes negative: the subject is over the limit and `fulfill` refuses until usage drops back below it.
- `top_up` also sets the allotment to the new amount, but leaves the balance alone on a downgrade: what the subject had stays until it is spent or the wallet expires. A later upgrade adds the difference against the downgraded plan, since the upgrade is paid for again.
- On a [windowed](/guide/sliding-windows) wallet both set the per-window allotment to the new amount. The window's history stays, so switching plans does not hand out a fresh window.
- A kept wallet is never archived for being drained — the next switch adjusts it.

The current wallet is replaced rather than kept when it has no allotment (it was opened under `replace`), or when one of the two is windowed and the other is not. A current plan wallet that no spec matches is closed.

A switch with an `adjust` or `top_up` spec changes balances immediately, so it cannot take a future `effective_date` and raises `ValueError` if given one. Run it on the day — from the same job that handles the billing period.

## Archival

Two things move wallets out of live storage. Neither deletes anything: the wallet and its whole transaction history are copied into archive tables first.

### Drained wallets

A wallet is archived the moment it is spent out — balance at zero or below, with no overdraft headroom left. It can't fund anything more, so keeping it in the table only makes every subsequent candidate query slower.

Not archived: wallets with a positive balance, wallets with an `overdraft_limit` (they can still pay), [sliding-window](/guide/sliding-windows) wallets (they replenish by design), and wallets with an `allotment` (the next plan switch or `adjust_wallet` funds them again).

::: tip Testing against a wallet you want to keep
Give it `overdraft_limit=Decimal("0")`. It survives being emptied without ever being allowed to go negative.
:::

### Expiry

An expired wallet is invisible to fulfillment, so nothing will ever draw from it again and the drain trigger will never see it. Those need sweeping:

```python
from allowances_django.shortcuts import archive_expired_wallets

archive_expired_wallets()   # returns how many were archived
```

Run it periodically — a Celery beat task, a cron job, a management command. It is safe to run often and does nothing when there is nothing to do.

Every `plan_switch` that replaces wallets leaves closed wallets behind, so on a system with regular plan changes this is what keeps the live table proportional to *active* subjects rather than to all history.

### What the archive holds

`WalletArchive` keeps the wallet's final state, when it was originally created, and when it was archived. `TransactionArchive` keeps its entries, including the `storno_of` links between them, rebuilt to point at the archived copies.

There is deliberately no foreign key from the archive back to the live wallet — the live row is gone by then. `original_wallet_id` is a plain reference, the way an audit table's is.

Archives are for reading. They are registered read-only in the Django admin, and nothing in the library ever moves a wallet back.
