# Concepts

Currencies and features are definition (configuration); wallets, transactions, fulfilment records, memberships and
modifiers are state.

## Definition vs. state

**Definition** is which rights exist: that a feature called `send_email` exists, that it costs one `email_send` or half
a unit of `credits`, that the user's wallets are tried before the team's. It is the same for every subject and is read
through a `DefinitionProvider` (`settings.ALLOWANCES` with Django).

**State** is what a particular subject holds and has consumed: a wallet with 47 credits in it, and the 153 transactions
that produced that balance.

Definitions are read through the [provider interface](/api/core#definitionprovider): from a dict, a per-tenant
configuration built per request, or a remote service.

## Subject

The holder of a licence: a user, an organisation, a device, a location, a physical machine.

The core receives only a `SubjectRef`, a `(type, id)` pair, not application objects. The Django adapter converts model
instances:

```python
fulfill("send_email", 1, {"user": request.user, "team": team})
```

The keys (`"user"`, `"team"`) are chosen by the application, and `fulfillment_order` refers to them. The same object
can have different roles in different features.

A subject may be a [member](#membership) of other subjects, and then also draws on their wallets.

::: tip Subject storage
A subject is stored as two strings (`"auth.user"`, `"42"`), not as a GenericForeignKey: the core does not depend on
Django, subjects with UUID and slug primary keys work, and schema-per-tenant deployments do not depend on the shared
`django_content_type` table. See [the Django backend notes](/backends/django#why-there-is-no-contenttype).
:::

## Currency

A named unit: `email_send`, `coolant_liters`, `credits`. Two flags:

```python
"credits": {"general": True, "round_fn": "ceil"},
```

`general` marks a fallback currency that any feature accepting it may draw from.

`round_fn` sets how a fractional cost becomes a whole unit: `"floor"`, `"ceil"`, `"round"`, or `None` to allow
fractions. Balances are `Decimal` throughout, at 20 digits with 6 decimal places, so amounts have no binary
floating-point error.

## Feature

A capability that consumes currency. It may have a `grant` section, a `consumable` section, or both:

```python
"send_email": {
    "grant": {"currencies": ["email_send"]},
    "consumable": {"currencies": ["email_send", ("credits", 0.5)]},
    "subjects": {"user", "team"},
    "fulfillment_order": [("grant", "user"), ("consumable", "user"), ("consumable", "team")],
    "overdraft": False,
},
```

A **grant** is a held entitlement, such as the subscription that permits the action. A **consumable** is a budget
that is consumed. A feature with both requires a grant and draws from a budget.

`("credits", 0.5)` is a conversion rate: one unit of this feature costs half a credit. A bare string means a rate of 1.

`subjects` documents which subject names the feature recognises. `overdraft` sets whether wallets may go below zero;
see [Overdraft](/guide/overdraft).

## Wallet

A subject's holding of one currency, of one type (grant or consumable). A subject can have several.

| Field | Meaning |
| --- | --- |
| `balance` | The amount it holds. For windowed wallets, see below. |
| `valid_from` / `valid_until` | When it counts. Outside this range it is invisible to fulfilment. |
| `overdraft_limit` | How far below zero it may go, or `None` for not at all. |
| `first_overdraft_at` | When it first went negative. Set once, never cleared. |
| `reset_config` | Makes it a [sliding-window](/guide/sliding-windows) wallet. |
| `source` | Where it came from. `plan_switch` only opens and closes wallets with source `"plan"`; a `"purchase"` or `"budget"` wallet is left alone by it. |
| `allotment` | The plan's amount, for a wallet opened with an `adjust` or `top_up` [switch policy](/guide/plans-and-archival#switch-policies). `None` otherwise. |

Within a fulfilment step, the wallet that expires soonest is drawn first and wallets without `valid_until` last, so
monthly plan credits are consumed before purchased credits that do not expire.

::: warning `balance` means two different things
Without a `reset_config` it is the stored balance, reduced by every consumption. With one it is a **per-window
allotment** that fulfilment does not change, and the balance that governs decisions is computed from the transactions
inside the current window. [Sliding windows](/guide/sliding-windows) describes it.
:::

## Transaction

One signed ledger entry against a wallet, of one of four kinds:

| Kind | Written by | Amount |
| --- | --- | --- |
| `consumption` | `fulfill` | Negative; zero for the use of a [right](/guide/permissions). |
| `grant` | `credit()`, or a plan adjustment | Positive for a credit; either sign for an adjustment. |
| `transfer` | `transfer()` | Negative on the source wallet, positive on the target, one pair per transfer. |
| `storno` | `unfulfill` | The negated amount of the entry it reverses. |

An entry records which feature caused it, which fulfilment record it belongs to, once attached which domain object
it paid for, and its **actor**: the subject on whose behalf it was made. `fulfill` records the subject it was called
for when there is exactly one, or the `actor` passed to it; a group wallet paying for a member's use therefore shows
the member as the actor. A storno carries the actor passed to `unfulfill`, i.e. who reversed it.

Transactions are not edited; on archival they are moved with their wallet to the archive. A reversal is a new `storno`
entry referencing the original. A transaction can be stornoed at most once; the Django store enforces this with a
unique constraint, `MemoryStore` with a check.

## FulfillmentRecord

The handle for one business operation, identified by a `guid`. Every transaction written by a `fulfill` call belongs
to one, and passing the same `guid` to several calls groups them:

```python
guid = uuid.uuid4()
fulfill("send_email", 1, subjects, guid=guid)
fulfill("attach_file", 3, subjects, guid=guid)

unfulfill(guid)  # both, as a unit
```

`unfulfill(guid)` reverses every transaction of the record. A `transfer` writes both of its legs under one record, so
`unfulfill` reverses it as a unit as well.

## Membership

A subject may be a member of other subjects. A group is an ordinary subject (a team, an organisation, a role) with no
special type; a subject may belong to any number of groups, and groups may be members of groups.

```python
from allowances_django.shortcuts import add_member, groups_of

add_member(user, team)
add_member(team, organisation)
groups_of(user)   # [team, organisation]
```

`peek` and `fulfill` expand every passed subject with its groups, under the same key: `{"user": user}` also draws on
the team's and the organisation's wallets, in the order described in
[Fulfilment order](/guide/fulfillment-order#memberships). A membership that would make a subject a member of itself,
directly or through other groups, raises `MembershipCycleError`.

Memberships are state: rows in the store, changed with `add_member` and `remove_member`.

## What a subject holds

`effective_allowances` lists everything a subject holds and where each holding comes from: every live wallet of the
subject and of its groups, of any currency and wallet type, with its effective balance.

```python
from allowances_django.shortcuts import effective_allowances

for allowance in effective_allowances(user):
    print(allowance.holder, allowance.depth, allowance.wallet.wallet_type,
          allowance.wallet.currency, allowance.balance)

# auth.user:5 0 consumable email_send 153.000000
# myapp.team:2 1 consumable email_send 1000.000000
# myapp.organisation:1 2 grant perm:club.view_member 1.000000
```

Each entry is an [`Allowance`](/api/core#allowance): the `wallet`, its `holder` (the subject itself or one of its
groups), the `depth` of the holder (0 for the subject, 1 for a direct group, 2 for a group of that group) and the
`balance` that governs decisions; for a [windowed](/guide/sliding-windows) wallet, the amount left in the current
window. The list is ordered nearest holder first. `wallet_types=` and `currencies=` narrow it; `at=` evaluates it at
another instant.

Wallets of every balance are listed, so an empty budget is listed with balance 0; filter on `balance > 0` for what can
still be consumed. Nothing is locked or written: the list is a snapshot, like `peek`.
[Django permissions](/guide/permissions#effective-permissions) are one filter of it.

## Modifier

A time-limited, subject-scoped factor that changes what a feature costs: a discount, a promotion, a penalty rate.
Where several apply, the highest factor is used.

```python
LicenseModifier.objects.create(
    subject_type="auth.user", subject_id=str(user.pk),
    feature="send_email", factor=Decimal("0.5"),
    valid_until=end_of_promo,
)
```

Modifiers are state: they belong to a subject and have a validity window.
