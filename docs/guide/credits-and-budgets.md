# Credits and budgets

A plan is one way to fund a wallet. This page has recipes for three others: prepaid credits, a postpaid budget billed
after use, and transfers between wallets. All three use these functions:

| | |
| --- | --- |
| `open_wallet(subject, spec, source=...)` | Opens one wallet outside a plan switch. Any `source` other than `"plan"` keeps it out of every `plan_switch`. |
| `credit(wallet, amount, obj=..., guid=...)` | Adds to a wallet's balance as a `grant` ledger entry, attached to whatever paid for it. |
| `transfer(from_wallet, to_wallet, amount)` | Moves an amount between two wallets of the same currency, as a pair of `transfer` ledger entries. |
| `close_wallet(wallet)` | Ends a wallet now, without a ledger entry. |
| `adjust_wallet(wallet, allotment)` | Changes the allotment of a wallet opened with an `adjust` or `top_up` policy — a limit raised or lowered. |
| `consumed(wallet, since, until)` | Net amount drawn from a wallet in a period, reversals deducted. |
| `effective_allowances(subject)` | Lists every wallet a subject holds, its own and its groups', with the effective balance — see [What a subject holds](/guide/concepts#what-a-subject-holds). |
| `(wallet_type, subject, source)` steps | Orders wallets of one subject and currency by where they came from. |

The library holds and enforces balances. Charging a card, issuing an invoice and setting prices are done by the host
application.

## Prepaid credits

The customer buys a pack of 500 credits. It stays until spent, whatever plan they move to.

```python
from decimal import Decimal

from django.db import transaction

from allowances import WalletSpec, WalletType
from allowances_django.shortcuts import credit, open_wallet


def credits_purchased(user, payment):
    with transaction.atomic():
        pack = open_wallet(
            user,
            WalletSpec(WalletType.CONSUMABLE, "credits", Decimal("0")),
            source="purchase",
        )
        credit(pack, payment.credits, obj=payment)
```

Each purchase is its own wallet. That gives every pack its own expiry: pass `valid_until` in the spec for credits that
expire (for example a year after purchase); omit it for credits that do not expire. Within a fulfilment step, each
subject's wallets are drawn soonest `valid_until` first and wallets without `valid_until` last, so a pack that expires
is spent before a permanent one; plan credits with a `valid_until` are ordered with the packs by that date. In a step
that covers several subjects (for example the user and its groups), the subjects are walked in order and the expiry
order applies within each one. A spent pack is archived like any drained wallet; an expired one by
`archive_expired_wallets`.

Opening the wallet empty and crediting it writes the funding to the ledger as a `grant` entry attached to the payment,
so the origin of each pack's credits can be queried.

A feature draws from packs by accepting their currency; no separate step is needed. To spend plan credits before any
pack regardless of expiry — needed when the plan credits are a [windowed](/guide/sliding-windows) wallet, which has no
`valid_until` — split by source:

```python
"fulfillment_order": [
    ("consumable", "user", "plan"),
    ("consumable", "user", "purchase"),
],
```

**Refunds.** Pass a `guid` to `credit` and the purchase can be reversed with `unfulfill(guid)`. The storno takes the
full amount back out of the pack, so a pack that has been partly spent ends below zero, and a pack spent out has been
archived and its storno is skipped; to refund only the unspent part, read `effective_balance` of the pack. The refund
policy is up to the host application.

**Plan credits combined with packs.** Give the plan's credits `on_switch=SwitchPolicy.TOP_UP` and an upgrade adds the
difference to them; the packs are not changed. See [switch policies](/guide/plans-and-archival#switch-policies).

## Monthly plan credits

Credits a plan grants every billing period are a per-period allotment. Two shapes, depending on whether unused credits
carry over:

**They don't carry over.** A windowed wallet with a monthly `reset_config` starts each period with its full allotment;
no scheduled job is needed:

```python
WalletSpec(
    WalletType.CONSUMABLE, "credits", Decimal("1000"),
    reset_config={"day_of_month": 1, "time": "00:00:00"},
    on_switch=SwitchPolicy.ADJUST,
)
```

**They carry over.** A stored-balance plan wallet with `on_switch=SwitchPolicy.TOP_UP`, credited by the billing job at
the start of each period. Under `top_up` a plan switch keeps the wallet, and with it the carried-over credits:

```python
credit(plan_wallet, Decimal("1000"), obj=invoice)
```

## Postpaid budget

The plan includes 300 requests a month. Beyond that the customer may spend up to a limit they set, and the application
bills what was used at the end of the month.

The included amount and the budget are two wallets of the same currency, told apart by source:

```python
from decimal import Decimal

from allowances import SwitchPolicy, WalletSpec, WalletType
from allowances_django.shortcuts import open_wallet, plan_switch

MONTHLY = {"day_of_month": 1, "time": "00:00:00"}

plan_switch({"user": user}, [
    WalletSpec(
        WalletType.CONSUMABLE, "requests", Decimal("300"),
        reset_config=MONTHLY, on_switch=SwitchPolicy.ADJUST,
    ),
])

budget = open_wallet(
    user,
    WalletSpec(
        WalletType.CONSUMABLE, "requests", Decimal("2000"),
        reset_config=MONTHLY, on_switch=SwitchPolicy.ADJUST,
    ),
    source="budget",
)
```

```python
"features": {
    "premium_request": {
        "consumable": {"currencies": ["requests"]},
        "fulfillment_order": [
            ("consumable", "user", "plan"),
            ("consumable", "user", "budget"),
        ],
    },
},
```

`fulfill` spends the included 300 first and the budget after it. When both are exhausted it raises
`InsufficientBalanceError`: the customer has reached the budget limit.

**The customer changes the limit.** `adjust_wallet(budget, Decimal("5000"))`. On a windowed budget this sets the new
monthly limit and keeps what was already used this month; a limit lowered below current usage refuses further requests
until the next month. A limit of zero turns overage off.

**Billing.** At the end of the period, the amount to charge is what was drawn from the budget wallet:

```python
from allowances_django.shortcuts import consumed

used = consumed(budget, period_start, period_end)
invoice_line(user, quantity=used, unit_price=PRICE_PER_REQUEST)
```

`consumed` counts consumption and deducts its reversals, so a request refunded with `unfulfill` within the period is not
billed. A reversal booked in a later period reduces that period's figure instead.

**Plan changes.** The budget has source `"budget"`, so `plan_switch` changes neither the wallet nor its limit. Only the
included amount follows the plan; under `adjust` it keeps this month's usage, so a switch in mid-month does not reset
the used amount to 0.

## Moving money between wallets

A tour guide holds a budget for the trip and gives each tourist 10 EUR of it to spend on their own. The tourist's money
is a wallet of its own, funded from the guide's:

```python
from decimal import Decimal

from django.db import transaction

from allowances import WalletSpec, WalletType
from allowances_django.shortcuts import open_wallet, transfer


def hand_out(guide_budget, tourist, guide):
    with transaction.atomic():
        pocket = open_wallet(
            tourist,
            WalletSpec(WalletType.CONSUMABLE, "eur", Decimal("0")),
            source="allowance",
        )
        transfer(guide_budget, pocket, Decimal("10"), actor=guide)
    return pocket
```

Opening and funding run in one transaction: when the guide's budget holds less than 10 EUR, `transfer` raises
`InsufficientBalanceError` and the empty wallet is rolled back with it. With the core engine the same block is `with
store.unit_of_work():`, which nests the same way.

`transfer` never draws on overdraft: the source must hold the amount. Both wallets must be in the same currency; their
types may differ. The ledger shows a `transfer` entry of −10 on the budget and +10 on the tourist's wallet, both with
the guide as actor.

**Returning what is left.** At the end of the trip the unspent money goes back with another transfer, in the opposite
direction:

```python
from django.db import transaction

from allowances.engine import effective_balance
from allowances_django.shortcuts import close_wallet, transfer
from allowances_django.store import DjangoStore


def settle(pocket, guide_budget, guide):
    store = DjangoStore()
    current = store.get_wallet(pocket.id)
    if current is None:          # spent out and archived
        return
    with transaction.atomic():
        left = effective_balance(store, current)
        if left > 0:
            transfer(current, guide_budget, left, actor=guide)
        close_wallet(current)
```

A tourist spending in the meantime leaves the pocket holding less than `left`; `transfer` then raises
`InsufficientBalanceError` and nothing is written, so the call can be repeated.

`unfulfill(result.guid)` also reverses a transfer, but only while the target still holds the whole amount (counting what
the same record gives back to it) and the source is live — see [Reversing a
transfer](/guide/reversals#reversing-a-transfer). A transfer is complete when it is written: it needs no `attach`, and
the recovery sweep never reports it.
