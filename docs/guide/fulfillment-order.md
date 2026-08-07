# Fulfillment order

When a request can be paid for from several places, something has to decide which to drain first. That decision is a business rule — spend the customer's own budget before the team pool, spend expiring grants before fresh ones, spend the promotional balance first so it doesn't go to waste — so it belongs in configuration rather than in a branch somewhere.

## The list form

```python
"fulfillment_order": [
    ("grant", "device"),      # the device's own grant wallets
    ("grant", None),          # then any subject's grant wallets
    ("consumable", "team"),   # then the team's consumable wallets
    "location",               # then any wallet type belonging to the location
],
```

Each step is `(wallet_type, subject_name)`:

- `wallet_type` is `"grant"`, `"consumable"`, or `None` for either.
- `subject_name` is one of the keys you pass to `fulfill()`, or `None` for every subject you passed, in the order you passed them.
- A bare string is shorthand: `"location"` means `(None, "location")`.

Steps are walked in order. Within one step, wallets come back **oldest first**, so a soon-to-expire grant is spent before a newly opened one.

Two things worth knowing:

**A wallet is only visited once.** If two steps would both match it, the earlier step wins and the later one skips it.

**A step naming a subject you didn't pass is skipped, not an error.** Features are declared once and called from contexts that legitimately know about different subjects — a background job may have the team but no user. Declaring `("consumable", "user")` and then calling without a `"user"` key simply moves on.

## Currencies within a step

A step matches a wallet only if the feature's section for that wallet type accepts the wallet's currency. Given:

```python
"consumable": {"currencies": ["email_send", ("credits", 0.5)]},
```

a step will draw from `email_send` wallets at one unit each and `credits` wallets at half a credit each — whichever it reaches first in the order. A wallet in a currency the section doesn't list is invisible to that step.

## The callable form

When the rule can't be expressed as a list — "drain the fattest wallet first", "prefer whichever expires soonest", "prefer the currency the customer has most of" — pass a callable instead:

```python
"fulfillment_order": "myapp.allowances.order_wallets",
```

```python
# myapp/allowances.py
def order_wallets(wallets):
    """Drain the fattest wallet first."""
    return sorted(wallets, key=lambda w: (-w.balance, w.id))
```

The contract:

- It receives a **`list[Wallet]`** — plain value objects, not a database query. This is what makes a custom ordering portable across backends.
- It returns anything iterable of those wallets, in draw order.
- It may filter as well as order. Dropping a wallet means it is never drawn from.
- It is called once per `fulfill()`, and both the ordinary and the [overdraft](/guide/overdraft) pass walk the same result.

The list it gets contains every wallet that could plausibly match: belonging to any subject you passed, of any wallet type the feature has a section for, in any currency those sections accept, valid right now. Ordering is your only job — validity and currency filtering already happened.

You may also pass the function object directly rather than a dotted path, which is handy in tests:

```python
"fulfillment_order": order_wallets,
```


## Modifiers apply on top

A [modifier](/guide/concepts#modifier) multiplies the cost after the rate but before rounding, and it applies uniformly across every wallet in the walk. It changes what things cost, never the order they're drawn in.

## Worked example

```python
"features": {
    "send_email": {
        "grant": {"currencies": ["email_send"]},
        "consumable": {"currencies": ["email_send", ("credits", 0.5)]},
        "subjects": {"user", "team"},
        "fulfillment_order": [
            ("grant", "user"),
            ("consumable", "user"),
            ("consumable", "team"),
        ],
    },
},
```

The user holds a grant wallet with 2 `email_send` and a consumable wallet with 10 `credits`; the team holds a consumable wallet with 100 `email_send`.

`fulfill("send_email", 30, {"user": user, "team": team})` draws:

| Step | Wallet | Taken | Covers |
| --- | --- | --- | --- |
| `("grant", "user")` | user's grant, 2 `email_send` | 2 | 2 emails |
| `("consumable", "user")` | user's 10 `credits` | 10 credits | 20 emails |
| `("consumable", "team")` | team's 100 `email_send` | 8 | 8 emails |

Thirty emails, three ledger entries, one fulfillment record. The user's own allocation is exhausted before the team pool is touched, which is what the order said to do.
