# Plans and archival

## Plan switch

Putting a subject on a different plan means two things happening together: everything they had stops, and the new set starts. If those come apart — even for a moment — the subject either has both plans or neither.

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

Every wallet the subject currently holds gets `valid_until` set, and the new set is opened with a matching `valid_from`. One transaction, all or nothing.

**Every spec is opened once per subject.** Passing two subjects and three specs opens six wallets — each subject gets their own. That is usually what a shared plan means.

**Already-closed wallets keep their original end date.** A wallet that expired last week is not re-closed with today's timestamp; its history stays accurate.

**Scheduling is just a future date:**

```python
plan_switch({"user": user}, specs, effective_date=next_billing_date)
```

The old wallets stay valid until then and the new ones aren't visible before it. Nothing needs to run on the day.

::: warning Balances do not carry over
The old wallets are closed with whatever was in them. If your plan change should preserve a remainder, read the balances first and fold them into the new specs. Nothing does that for you, because whether unused quota rolls over is a commercial decision, not a technical default.
:::

## Archival

Two things move wallets out of live storage. Neither deletes anything: the wallet and its whole transaction history are copied into archive tables first.

### Drained wallets

A wallet is archived the moment it is spent out — balance at zero or below, with no overdraft headroom left. It can't fund anything more, so keeping it in the table only makes every subsequent candidate query slower.

Not archived: wallets with a positive balance, wallets with an `overdraft_limit` (they can still pay), and [sliding-window](/guide/sliding-windows) wallets (they replenish by design).

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

Every `plan_switch` leaves closed wallets behind, so on a system with regular plan changes this is what keeps the live table proportional to *active* subjects rather than to all history.

### What the archive holds

`WalletArchive` keeps the wallet's final state, when it was originally created, and when it was archived. `TransactionArchive` keeps its entries, including the `storno_of` links between them, rebuilt to point at the archived copies.

There is deliberately no foreign key from the archive back to the live wallet — the live row is gone by then. `original_wallet_id` is a plain reference, the way an audit table's is.

Archives are for reading. They are registered read-only in the Django admin, and nothing in the library ever moves a wallet back.
