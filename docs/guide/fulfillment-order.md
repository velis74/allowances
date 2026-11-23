# Fulfilment order

When several wallets can pay for a request, the feature's `fulfillment_order` sets the order in which they are drawn
from. It is configuration per feature, for example the user's own budget before the team pool, grants that expire
before fresh ones, or a promotional balance first.

## The list form

```python
"fulfillment_order": [
    ("grant", "device"),      # the device's own grant wallets
    ("grant", None),          # then any subject's grant wallets
    ("consumable", "team"),   # then the team's consumable wallets
    "location",               # then any wallet type belonging to the location
],
```

Each step is `(wallet_type, subject_name)` or `(wallet_type, subject_name, source)`:

- `wallet_type` is `"grant"`, `"consumable"`, or `None` for either.
- `subject_name` is one of the keys you pass to `fulfill()`, or `None` for every subject you passed, in the order you
  passed them.
- `source` limits the step to wallets with that [`source`](/guide/concepts#wallet) — `"plan"`, `"purchase"`,
  `"budget"`. Omitted or `None`, the step matches every source.
- A bare string is shorthand: `"location"` means `(None, "location", None)`.

Steps are walked in order. Within one subject of a step, the wallet that **expires soonest** is drawn first, wallets
without `valid_until` last, and wallets with the same expiry oldest first. Monthly plan credits are therefore spent
before purchased credits that never expire, and a grant that expires soon before a fresh one.

Splitting one subject's wallets by source puts the plan's included amount ahead of a paid budget in the same currency:

```python
"fulfillment_order": [
    ("consumable", "user", "plan"),
    ("consumable", "user", "budget"),
],
```

**A wallet is visited only once.** If two steps match it, the earlier step draws from it and the later one skips it.

**A step naming a subject you did not pass is skipped, not an error.** With the step `("consumable", "user")` and no
`"user"` key in the call (for example a background job that has the team but no user), the walk continues with the
next step.

## Memberships

`fulfill` and `peek` expand every subject you pass with the groups it is a [member](/guide/concepts#membership) of,
under the same key. The key `"user"` then holds an ordered list: the user, its direct groups, then their groups,
breadth-first, each group once. Without memberships the list holds only the subject.

- A step naming a key walks that key's list in order. Within each subject of the list, wallets are ordered by expiry
  as above, so `("consumable", "user")` draws from the user's own wallets before the team's, and from the team's before
  the organisation's.
- A step with subject `None` walks every expanded subject: each key's list, in the order you passed the keys.
- A callable `fulfillment_order` receives the candidates of every expanded subject.
- [Modifiers](#modifiers) are looked up for every expanded subject; the smallest factor applies.
- A wallet is drawn at most once, even when a group is reachable from two keys.

Given `add_member(user, team)`, `fulfill("send_email", 1, {"user": user})` with the order `[("consumable", "user")]`
draws from the user's wallets first and from the team's after them. Passing the team as its own key,
`{"user": user, "team": team}`, places it where the order names that key.

## Currencies within a step

A step matches a wallet only if the feature's section for that wallet type accepts the wallet's currency. Given:

```python
"consumable": {"currencies": ["email_send", ("credits", 0.5)]},
```

a step draws from `email_send` wallets at one unit each and from `credits` wallets at half a credit each, in the order
it reaches them. A step does not draw from a wallet whose currency the section does not list.

## The callable form

For an order a list cannot express, for example largest balance first or the currency with the largest total balance
first, pass a callable:

```python
"fulfillment_order": "myapp.allowances.order_wallets",
```

```python
# myapp/allowances.py
def order_wallets(wallets):
    """Largest balance first."""
    return sorted(wallets, key=lambda w: (-w.balance, w.id))
```

The contract:

- It receives a **`list[Wallet]`** — plain value objects, not a database query. The same function therefore works
  with every backend.
- It returns an iterable of those wallets, in draw order.
- It may filter as well as order. A wallet it drops is not drawn from.
- It is called once per `fulfill()`, and both the ordinary and the [overdraft](/guide/overdraft) pass walk the same
  result.

The list contains every wallet that can match: belonging to any subject you passed or any of its groups, of any
wallet type the feature has a section for, in any currency those sections accept, valid at the time of the call. The
list is already filtered by validity and currency.

The value can also be the function object instead of a dotted path:

```python
"fulfillment_order": order_wallets,
```

## Modifiers

A [modifier](/guide/concepts#modifier) multiplies the cost after the rate but before rounding, and it applies equally
to every wallet in the walk. Where several apply, the smallest factor is used. A modifier does not change the draw
order.

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

The user holds a grant wallet with 2 `email_send` and a consumable wallet with 10 `credits`; the team holds a
consumable wallet with 100 `email_send`.

`fulfill("send_email", 30, {"user": user, "team": team})` draws:

| Step | Wallet | Taken | Covers |
| --- | --- | --- | --- |
| `("grant", "user")` | user's grant, 2 `email_send` | 2 | 2 emails |
| `("consumable", "user")` | user's 10 `credits` | 10 credits | 20 emails |
| `("consumable", "team")` | team's 100 `email_send` | 8 | 8 emails |

The call writes three consumption entries under one fulfilment record.
