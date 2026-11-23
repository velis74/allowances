# Overdraft

Overdraft lets a wallet's balance go below zero, down to a per-wallet limit.

## Enabling overdraft

Overdraft happens only when **both** are true:

```python
# 1. The feature permits it.
"send_email": {
    # ...
    "overdraft": True,
},
```

```python
# 2. The wallet has an explicit limit.
WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("50"), overdraft_limit=Decimal("20"))
```

`overdraft_limit=None` (the default) disables overdraft for the wallet whatever the feature says. `overdraft: True` on
a feature therefore affects only wallets with a limit; a configuration change alone does not let any balance go
negative.

## How the passes work

`fulfill` walks the [fulfilment order](/guide/fulfillment-order) twice.

**First pass:** ordinary balances only. Each wallet can contribute up to `max(balance, 0)`.

**Second pass:** only if part of the amount is still uncovered *and* the feature has `overdraft: True`. This pass
visits the same wallets in the same order, skips any with `overdraft_limit=None`, and lets each contribute up to
`max(balance, 0) + overdraft_limit`, minus what the first pass already took from it.

A wallet holding 5 with a limit of 3 can therefore fund 8 in total, and a request for 7 produces two entries — `-5`
from the first pass and `-2` from the second — leaving the balance at `-2`.

Every positive balance in the order is used before any wallet goes below zero: with an empty user wallet and a funded
team wallet, the team wallet pays.

## Recording it

The first time a wallet's balance goes negative, `first_overdraft_at` is set. It is never cleared, even when the
balance returns above zero.

It gives the time since the wallet first went negative, for example for a dunning or suspension job. The library takes
no action on it.

```python
from allowances_django.models import Wallet

stale = Wallet.objects.filter(
    first_overdraft_at__lt=timezone.now() - timedelta(days=14),
    balance__lt=0,
)
```

## Interaction with archival

A wallet without an `overdraft_limit` that is drained to zero or below is
[archived immediately](/guide/plans-and-archival#drained-wallets), unless it is windowed or has an `allotment`.

A wallet with an `overdraft_limit` (including `0`) is never archived when drained, whatever its balance. In tests,
`overdraft_limit=Decimal("0")` keeps an emptied wallet in live storage without allowing a negative balance.

## What overdraft does not do

The library neither refills the wallet nor bills for the negative amount. The balance stays negative until a `credit`,
`adjust_wallet` or [plan switch](/guide/plans-and-archival#plan-switch) changes it.
