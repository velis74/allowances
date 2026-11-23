# Plans and archival

## Plan switch

`plan_switch` closes the subject's current plan wallets and opens the new plan's wallets in one transaction.

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

Every plan wallet the subject holds gets `valid_until` set, except those kept by an `adjust` or `top_up`
[spec](#switch-policies), and the new wallets are opened with a matching `valid_from`. One transaction, all or nothing.

**Only plan wallets take part.** `plan_switch` opens wallets with `source="plan"` and closes only those. A wallet opened
with [`open_wallet`](/guide/credits-and-budgets) under another source (a purchased credit pack, a spending budget)
keeps its balance and validity through every plan change.

**Every spec is opened once per subject.** Two subjects and three specs open six wallets.

**Already-closed wallets keep their original end date.** A wallet that expired last week keeps its `valid_until`.

**A future `effective_date` schedules the switch:**

```python
plan_switch({"user": user}, specs, effective_date=next_billing_date)
```

The old wallets stay valid until then, the new ones are not visible before it, and a kept wallet's
[adjustment](#switch-policies) applies from that instant. No job runs on the day. A downgrade or a cancellation at the
end of a paid period is a scheduled switch; a cancellation is a switch to an empty plan:

```python
plan_switch({"user": user}, [], effective_date=period_end)
```

**A scheduled switch can be cancelled** until it takes effect:

```python
from allowances_django.shortcuts import cancel_scheduled_switch

cancel_scheduled_switch({"user": user})   # returns how many subjects had one
```

Wallets it would have closed get their previous `valid_until` back, adjustments it scheduled are dropped, and wallets
it would have opened end before they begin (`archive_expired_wallets` archives them). A switch that has taken effect is
not cancelled; switch again instead.

**A new switch replaces a scheduled one.** Calling `plan_switch` for a subject with a pending switch cancels the
pending one first, whether the new switch is immediate or scheduled.

A scheduled switch records what it changes on each wallet it affects (`Wallet.scheduled`). An adjustment is applied
lazily: `peek` and `effective_balance` see it from its effective date on, and the first operation that writes to the
wallet after that date books it, dated on the effective date.

## Switch policies

Each `WalletSpec` sets with `on_switch` what happens to an existing plan wallet:

| Policy | The subject's current plan wallet of the same type and currency | Fits |
| --- | --- | --- |
| `SwitchPolicy.REPLACE` (default) | Closed with whatever was left in it; a new wallet opens with the spec's balance. A later reversal of earlier consumption goes back to the closed wallet. | Monthly quotas that start over with the new plan; boolean grants. |
| `SwitchPolicy.ADJUST` | Kept; its balance moves by *new allotment − old allotment*, which may be negative. | Capacities (storage, seats) where usage must carry over to the new plan; rate quotas. |
| `SwitchPolicy.TOP_UP` | Kept; it receives only a positive difference. | Plan credits: an upgrade adds the difference to the larger plan, a downgrade takes nothing away. |

```python
from allowances import SwitchPolicy

plan_switch({"user": user}, [
    WalletSpec(WalletType.CONSUMABLE, "storage_gb", Decimal("100"), on_switch=SwitchPolicy.ADJUST),
    WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("3000"), on_switch=SwitchPolicy.TOP_UP),
    WalletSpec(WalletType.GRANT, "api_access", Decimal("1")),
])
```

A user on 10 GB with 8 GB in use who moves to 100 GB has 92 GB left; a reversal of an upload made before the switch
(deleting the file) frees space in the same wallet. A user with 300 of 1000 plan credits left who moves to the 3000
plan has 2300.

The calculation needs the old plan's amount, so a wallet opened under `adjust` or `top_up` records it as `allotment`:

- `adjust` sets the allotment to the new amount. On a downgrade below current usage the balance goes negative: the
  subject is over the limit and `fulfill` refuses until usage drops back below it.
- `top_up` also sets the allotment to the new amount, but leaves the balance unchanged on a downgrade: the remaining
  balance stays until it is spent or the wallet expires. A later upgrade adds the difference against the downgraded
  plan, as the upgrade is paid for again.
- On a [windowed](/guide/sliding-windows) wallet both set the per-window allotment to the new amount. The window's
  entries stay, so a plan switch does not start a new window.
- A kept wallet is never archived when drained; the next switch adjusts it.

The current wallet is replaced rather than kept when it has no allotment (it was opened under `replace`), or when one
of the two is windowed and the other is not. A current plan wallet that no spec matches is closed.

A scheduled `adjust` or `top_up` computes its difference on the effective date, against the allotment the wallet has
then, so usage and `adjust_wallet` calls in between are included.

## Archival

Two operations move wallets out of live storage. Neither deletes anything: the wallet and its whole transaction
history are copied into archive tables first.

### Drained wallets

A wallet is archived when a draw leaves its balance at zero or below and it has no `overdraft_limit`.

Not archived: wallets with a positive balance, wallets with an `overdraft_limit` (including `0`),
[sliding-window](/guide/sliding-windows) wallets (the window restores their balance), and wallets with an `allotment`
(the next plan switch or `adjust_wallet` funds them again).

::: tip Testing against a wallet you want to keep
Give it `overdraft_limit=Decimal("0")`. It stays in live storage when emptied and cannot go negative.
:::

### Expiry

An expired wallet is not drawn from and is not archived by the drain rule. `archive_expired_wallets` archives it:

```python
from allowances_django.shortcuts import archive_expired_wallets

archive_expired_wallets()   # returns how many were archived
```

Run it periodically: a Celery beat task, a cron job, a management command. Running it repeatedly is safe.

Closed wallets left by `plan_switch` stay in live storage until it runs.

### What the archive holds

`WalletArchive` keeps the wallet's final state, when it was originally created, and when it was archived.
`TransactionArchive` keeps its entries, including the `storno_of` links between them, rebuilt to point at the archived
copies.

`original_wallet_id` is a plain id, not a foreign key: the live row no longer exists.

Archives are registered read-only in the Django admin, and the library never moves a wallet back.
