# Overdraft

Sometimes refusing is worse than going slightly negative. Cutting off a customer's transactional email mid-signup to save them two cents is a bad trade. Overdraft lets a wallet be pushed below zero, deliberately and within a limit.

## Two things have to agree

Overdraft only happens when **both** are true:

```python
# 1. The feature permits it at all.
"send_email": {
    # ...
    "overdraft": True,
},
```

```python
# 2. The wallet has an explicit limit.
WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("50"), overdraft_limit=Decimal("20"))
```

`overdraft_limit=None` means never, regardless of what the feature says. This is the default, and it is why turning `overdraft` on for a feature does nothing until you also open wallets that allow it — a deliberate double lock, so a config change can't quietly let every balance in the system go negative.

## How the passes work

`fulfill` walks the [fulfillment order](/guide/fulfillment-order) twice.

**First pass:** ordinary balances only. Each wallet can contribute up to `max(balance, 0)`.

**Second pass:** only if something is still uncovered *and* the feature has `overdraft: True`. This pass visits the same wallets in the same order, skips any with `overdraft_limit=None`, and lets each contribute up to `max(balance, 0) + overdraft_limit`, minus whatever the first pass already took from it.

So a wallet holding 5 with a limit of 3 can fund 8 in total, and a request for 7 produces two entries — `-5` from the first pass and `-2` from the second — leaving the balance at `-2`.

The two-pass shape matters: it means every wallet's *real* balance is exhausted before anyone's overdraft is touched. A customer with an empty personal budget and a healthy team pool spends the team pool rather than going into debt.

## Recording it

The first time a wallet's balance goes negative, `first_overdraft_at` is stamped. It is never cleared, even when the balance recovers.

That field exists to answer "how long has this account been in the red", which is the question a dunning or suspension flow actually needs. The library records it and takes no action; deciding what a fortnight of overdraft means is your policy, not this library's.

```python
from allowances_django.models import Wallet

stale = Wallet.objects.filter(
    first_overdraft_at__lt=timezone.now() - timedelta(days=14),
    balance__lt=0,
)
```

## Interaction with archival

A wallet drained to exactly zero is normally [archived on the spot](/guide/plans-and-archival#drained-wallets) — it can't pay for anything more, so it leaves the live tables.

A wallet with an `overdraft_limit` is not, even at zero, because it can still fund the next request. This is also the trick for keeping a wallet inspectable in tests: give it `overdraft_limit=Decimal("0")` and it survives being emptied without ever actually being allowed to go negative.

## What overdraft is not

It does not create balance. Nothing tops the wallet back up, and nothing bills for the shortfall. A negative balance simply stays negative until you grant more or [switch the plan](/guide/plans-and-archival#plan-switch). What that debt means commercially is your application's business.
