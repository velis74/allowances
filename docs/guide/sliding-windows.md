# Sliding windows

"200 emails a day" is a different thing from "200 emails". The first replenishes; the second doesn't.

The obvious implementation is a scheduled job that resets balances at midnight. This library doesn't do that, because a reset job is a liability: it can fail, it can run twice, it can run late, it has to know about every wallet in the system, and it destroys the very history you wanted the ledger for.

## How it works instead

Give a wallet a `reset_config` and two things change:

1. Its stored `balance` becomes a **per-window allotment** — a constant that fulfillment never modifies.
2. Its effective balance is that allotment **plus every transaction inside the current window**.

Consumption entries are negative, so the balance nets down as the window fills. When the window moves on, old entries fall outside it and stop counting — the quota appears to reset, without anything having written to anything.

```python
WalletSpec(
    WalletType.CONSUMABLE, "email_send", Decimal("200"),
    reset_config={"time": "00:00:00"},
)
```

Nothing needs to run at midnight. There is no job to monitor, and at 00:00 the wallet's row is byte-for-byte what it was at 23:59.

## Configuration shapes

| Shape | Meaning |
| --- | --- |
| `{"time": "00:00:00"}` | Daily, resetting at that UTC time. |
| `{"day_of_week": 0, "time": "00:00:00"}` | Weekly. `0` is Monday, following `date.weekday()`. |
| `{"day_of_month": 1, "time": "00:00:00"}` | Monthly, clamped to the last real day of shorter months. |
| `{"window_seconds": 3600}` | A rolling window with no calendar anchor — the last hour, continuously. |
| `{}` | Rolling, defaulting to 24 hours. |
| `None` | Not a windowed wallet at all. Balance is a plain stored amount. |

`day_of_month: 31` in February means the 28th — or the 29th in a leap year — rather than an error.

All arithmetic is UTC. A naive datetime handed in is assumed to already be UTC rather than rejected.

## Calendar reset vs. rolling window

The distinction matters for how it feels to a user.

A **calendar reset** (`time`, `day_of_week`, `day_of_month`) gives everyone the same boundary. Spend all 200 at 23:55 and you have 200 again five minutes later. Predictable, and occasionally exploitable.

A **rolling window** (`window_seconds`) is per-transaction. Spend 200 at 23:55 and you get them back at 23:55 the following day, one at a time as each entry ages out. Harder to game, harder to explain to a customer.

Calendar resets suit quotas people plan around ("your monthly allowance"). Rolling windows suit abuse prevention ("no more than 200 an hour").

## What this costs

Every fulfillment against a windowed wallet sums that wallet's transactions inside the window. In Postgres that is one aggregate over `(wallet_id, created_at)`, which the Django backend indexes for exactly this reason. It is cheap at the volumes most applications have and it is not free: a wallet with millions of entries per window will feel it. If you get there, the answer is a periodic compaction that archives aged-out entries — the archive tables already exist for it.

## Things that behave differently

**Windowed wallets are never archived for being spent out.** A balance at zero is a normal state for them; they replenish by design. They are only archived when they [expire](/guide/plans-and-archival#expiry).

**`first_overdraft_at` still gets set.** A windowed wallet can go negative within a window if the feature allows overdraft, and the first time it does is still recorded.

**Reversals land in whichever window they happen in.** Reverse inside the same window and the storno nets the consumption straight back out. Reverse *after* the window has passed and the original consumption has already aged out, so the storno is a positive entry in the *new* window — it credits a window that never paid. This is the honest behaviour for an append-only ledger, but it is worth knowing before you build a recovery flow that runs on a long delay.

## Checking a balance

```python
from allowances.engine import effective_balance
from allowances_django.store import DjangoStore

store = DjangoStore()
wallet = store.find_wallets(...)[0]

effective_balance(store, wallet)   # what fulfillment will actually see
wallet.balance                     # the allotment — usually not what you want to show a user
```

For a non-windowed wallet the two are the same.
