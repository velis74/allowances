# Sliding windows

A wallet with a `reset_config` holds an amount per time window (for example 200 emails a day); a wallet without it
holds a fixed amount. No scheduled job resets windowed balances: the balance is computed from the ledger entries inside
the current window, so no entry is overwritten.

## Windowed balance

Give a wallet a `reset_config` and two things change:

1. Its stored `balance` becomes a **per-window allotment**. Fulfilment never modifies it; only a plan switch with an
   `adjust` or `top_up` [policy](/guide/plans-and-archival#switch-policies) and `adjust_wallet` set a new one.
2. Its effective balance is that allotment **plus every transaction inside the current window**.

Consumption entries are negative, so the balance decreases as the window fills. Entries older than the window no
longer count, so the amount becomes available again without any write.

```python
WalletSpec(
    WalletType.CONSUMABLE, "email_send", Decimal("200"),
    reset_config={"time": "00:00:00"},
)
```

## Configuration shapes

| Shape | Meaning |
| --- | --- |
| `{"time": "00:00:00"}` | Daily, resetting at that UTC time. |
| `{"day_of_week": 0, "time": "00:00:00"}` | Weekly. `0` is Monday, following `date.weekday()`. |
| `{"day_of_month": 1, "time": "00:00:00"}` | Monthly, clamped to the last day of shorter months. |
| `{"window_seconds": 3600}` | A rolling window with no calendar anchor: the last hour, continuously. |
| `{}` | Rolling, 24 hours by default. |
| `None` | Not a windowed wallet. The balance is a plain stored amount. |

`day_of_month: 31` in February means the 28th, or the 29th in a leap year; it is not an error.

All arithmetic is UTC. A naive datetime is treated as UTC.

## Calendar reset vs. rolling window

A **calendar reset** (`time`, `day_of_week`, `day_of_month`) gives every wallet the same boundary. Spending 200 at
23:55 makes 200 available again at 00:00, so up to 400 can be used within minutes across the boundary.

A **rolling window** (`window_seconds`) is per transaction. Each consumed amount becomes available again when its entry
is older than the window: 200 spent at 23:55 return at 23:55 the next day.

Calendar resets suit quotas users plan around ("your monthly allowance"). Rolling windows suit rate limits ("no more
than 200 an hour").

## Performance

Every fulfilment against a windowed wallet sums that wallet's transactions inside the window. The Django backend has
an index on `(wallet, created_at)` for this query. The cost grows with the number of entries in the window.

## Archival, overdraft and reversals

**Windowed wallets are not archived when drained.** A balance at zero is a normal state for them, as the window
restores it. They are archived only when they [expire](/guide/plans-and-archival#expiry).

**`first_overdraft_at` is set.** A windowed wallet can go negative within a window if the feature allows overdraft,
and the first time it does is recorded.

**Reversals are booked in the window in which they happen.** A reversal inside the same window cancels the consumption.
After the window has passed, the original consumption no longer counts, so the storno is a positive entry in the new
window and raises that window's balance above its allotment. A reversal delayed by more than the window therefore adds
to the current window.

## Checking a balance

```python
from allowances.engine import effective_balance
from allowances_django.store import DjangoStore

store = DjangoStore()
wallet = (await store.find_wallets(...))[0]

await effective_balance(store, wallet)   # balance used by fulfillment
wallet.balance                           # per-window allotment
```

For a wallet without a window the two are the same.
