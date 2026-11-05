# Credits and budgets

A plan is one way to fund a wallet. Two others are common enough to have recipes: credits a customer buys up front, and a budget a customer may spend beyond the plan and is billed for afterwards. Both use the same pieces:

| | |
| --- | --- |
| `open_wallet(subject, spec, source=...)` | Opens one wallet outside a plan switch. Any `source` other than `"plan"` keeps it out of every `plan_switch`. |
| `credit(wallet, amount, obj=..., guid=...)` | Adds to a wallet's balance as a `grant` ledger entry, attached to whatever paid for it. |
| `adjust_wallet(wallet, allotment)` | Changes the allotment of a wallet opened with an `adjust` or `top_up` policy — a limit raised or lowered. |
| `consumed(wallet, since, until)` | Net amount drawn from a wallet in a period, reversals deducted. |
| `(wallet_type, subject, source)` steps | Orders wallets of one subject and currency by where they came from. |

The library holds and enforces balances. Charging a card, issuing an invoice and deciding prices stay in the host application; these recipes show where the two meet.

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

Each purchase is its own wallet. That gives every pack its own expiry: pass `valid_until` in the spec for credits that lapse (a year after purchase, say), leave it out for credits that never do. Within a fulfillment step the engine draws the soonest-expiring wallet first and wallets without `valid_until` last, so a lapsing pack is spent before a permanent one, and plan credits with a `valid_until` before both. A spent pack is archived like any drained wallet; an expired one by `archive_expired_wallets`.

Opening the wallet empty and crediting it books the funding in the ledger, attached to the payment, so "where did these credits come from" is a query rather than a guess.

A feature draws from packs by accepting their currency; no separate step is needed. To spend plan credits before any pack regardless of expiry — needed when the plan credits are a [windowed](/guide/sliding-windows) wallet, which has no `valid_until` — split by source:

```python
"fulfillment_order": [
    ("consumable", "user", "plan"),
    ("consumable", "user", "purchase"),
],
```

**Refunds.** Pass a `guid` to `credit` and the purchase can be reversed with `unfulfill(guid)`. The storno takes the full amount back out of the pack, so a pack that has been partly spent ends below zero, and a pack spent out has been archived and its storno is skipped; whether to refund only the unspent part is a commercial decision, and `effective_balance` of the pack is the number to make it with.

**Plan credits combined with packs.** Give the plan's credits `on_switch=SwitchPolicy.TOP_UP` and an upgrade adds the difference to them; the packs are never touched. See [switch policies](/guide/plans-and-archival#switch-policies).

## Monthly plan credits

Credits a plan grants every billing period are a per-period allotment. Two shapes, depending on whether unused credits carry over:

**They don't carry over.** A windowed wallet with a monthly `reset_config` refills itself at the start of each period, with nothing running on the day:

```python
WalletSpec(
    WalletType.CONSUMABLE, "credits", Decimal("1000"),
    reset_config={"day_of_month": 1, "time": "00:00:00"},
    on_switch=SwitchPolicy.ADJUST,
)
```

**They carry over.** A stored-balance plan wallet with `on_switch=SwitchPolicy.TOP_UP`, credited by the billing job at the start of each period. Under `top_up` a plan switch keeps the wallet, and with it the carried-over credits:

```python
credit(plan_wallet, Decimal("1000"), obj=invoice)
```

## Postpaid budget

The plan includes 300 requests a month. Beyond that the customer may spend up to a limit they set, and the application bills what was used at the end of the month.

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

`fulfill` spends the included 300 first and the budget after it. When both are exhausted it raises `InsufficientBalanceError`, which is the spending limit doing its job.

**The customer changes the limit.** `adjust_wallet(budget, Decimal("5000"))`. On a windowed budget this sets the new monthly limit and keeps what was already used this month; a limit lowered below current usage refuses further requests until the next month. A limit of zero turns overage off.

**Billing.** At the end of the period, the amount to charge is what was drawn from the budget wallet:

```python
from allowances_django.shortcuts import consumed

used = consumed(budget, period_start, period_end)
invoice_line(user, quantity=used, unit_price=PRICE_PER_REQUEST)
```

`consumed` counts consumption and deducts its reversals, so a request refunded with `unfulfill` within the period is not billed. A reversal booked in a later period reduces that period's figure instead.

**Plan changes.** The budget has source `"budget"`, so `plan_switch` leaves it — and its limit — alone. Only the included amount changes with the plan; under `adjust` it keeps this month's usage, so a switch mid-month does not hand out a fresh 300.
