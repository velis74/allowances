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

The old wallets stay valid until then and the new ones aren't visible before it, and a kept wallet's [adjustment](#switch-policies) applies from that instant. Nothing needs to run on the day. This is how a downgrade or a cancellation at the end of a paid period is expressed — a cancellation is a switch to an empty plan:

```python
plan_switch({"user": user}, [], effective_date=period_end)
```

**A scheduled switch can be cancelled** until it takes effect:

```python
from allowances_django.shortcuts import cancel_scheduled_switch

cancel_scheduled_switch({"user": user})   # returns how many subjects had one
```

Wallets it would have closed get their previous `valid_until` back, adjustments it scheduled are dropped, and wallets it would have opened end before they begin (`archive_expired_wallets` sweeps them). A switch that has taken effect is not cancelled; switch again instead.

**A new switch replaces a scheduled one.** Calling `plan_switch` for a subject with a switch still pending cancels the pending one first, whether the new switch is immediate or scheduled — the customer changed their mind.

A scheduled switch records what it changes on each wallet it affects (`Wallet.scheduled`). An adjustment is applied lazily: `peek` and `effective_balance` see it from its effective date on, and the first operation that writes to the wallet after that date books it, dated on the effective date.

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

A scheduled `adjust` or `top_up` computes its difference on the effective date, against the allotment the wallet has then, so usage and `adjust_wallet` calls in between are accounted for.

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
