# Concepts

Six ideas. Three of them are definition (configuration), three are state (rows).

## Definition vs. state

The split is the reason the library holds together, so it's worth stating plainly.

**Definition** is what rights exist at all: that a feature called `send_email` exists, that it costs one `email_send` or half a `credit`, that the user's wallets are tried before the team's. It is the same for everybody, it changes when you deploy, and it lives in `settings.ALLOWANCES` — not in the database.

**State** is what a particular subject currently holds and has spent: a wallet with 47 credits in it, and the 153 transactions that got it there.

Putting definitions in the database is a tempting mistake. It turns a config change into a data migration, makes "what did this cost last March" ambiguous, and means every environment drifts. Keeping them out means the definition layer can come from anywhere — a dict, a per-tenant config assembled at request time, a remote licence server — behind one small [provider interface](/api/core#definitionprovider).

## Subject

Whatever holds a licence. A user, an organisation, a device, a location, a physical machine.

The core never sees your objects — only a `SubjectRef`, a `(type, id)` pair. The Django adapter converts instances for you:

```python
fulfill("send_email", 1, {"user": request.user, "team": team})
```

Those keys — `"user"`, `"team"` — are names *you* choose, and they are what `fulfillment_order` refers to. The same physical object can play different roles in different features.

::: tip Why not a GenericForeignKey?
Because the core would then depend on Django. Storing `("auth.user", "42")` as two strings also means subjects with UUID or slug primary keys work, and a schema-per-tenant deployment stops fighting the shared `django_content_type` table. See [the Django backend notes](/backends/django#why-there-is-no-contenttype).
:::

## Currency

A named unit: `email_send`, `coolant_liters`, `credits`. Two flags:

```python
"credits": {"general": True, "round_fn": "ceil"},
```

`general` marks a fallback currency that any feature accepting it may draw from — the difference between "email quota" and "credits, which buy anything".

`round_fn` decides how a fractional cost becomes a whole unit: `"floor"`, `"ceil"`, `"round"`, or `None` to allow fractions. Balances are `Decimal` throughout, at 20 digits with 6 decimal places, so 3.27 credits is exactly 3.27 and never 3.2699999999999996.

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

A **grant** is an entitlement you hold — the subscription that says you may do this at all. A **consumable** is a budget you spend. Most interesting features have both: a plan that permits the action *and* caps how much of it you get.

`("credits", 0.5)` is a conversion rate: one unit of this feature costs half a credit. A bare string means a rate of 1.

`subjects` documents which subject names the feature recognises. `overdraft` decides whether wallets may be pushed below zero — see [Overdraft](/guide/overdraft).

## Wallet

A subject's holding of one currency, of one type (grant or consumable). A subject typically has several.

| Field | Meaning |
| --- | --- |
| `balance` | What's in it. For windowed wallets, see below. |
| `valid_from` / `valid_until` | When it counts. Outside this range it is invisible to fulfillment. |
| `overdraft_limit` | How far below zero it may go, or `None` for not at all. |
| `first_overdraft_at` | When it first went negative. Set once, never cleared. |
| `reset_config` | Makes it a [sliding-window](/guide/sliding-windows) wallet. |

Wallets are drawn oldest-first within a fulfillment step, so a grant that expires soon gets spent before a fresh one.

::: warning `balance` means two different things
Without a `reset_config` it is the actual stored balance, drawn down by every consumption. With one it is a **per-window allotment** that fulfillment never touches, and the balance that governs decisions is computed from the transactions inside the current window. This trips people up exactly once. [Sliding windows](/guide/sliding-windows) explains it.
:::

## Transaction

One signed ledger entry against a wallet: negative for consumption, positive for a grant or a storno. It records which feature caused it, which fulfillment record it belongs to, and — once attached — which domain object it paid for.

Nothing is ever edited in place and nothing is deleted while it still matters. A reversal is a new `storno` entry pointing at the original, and a transaction can be stornoed at most once, enforced by the database rather than by hope.

## FulfillmentRecord

The handle for one business operation, identified by a `guid`. Every transaction written by a `fulfill` call belongs to one, and passing the same `guid` to several calls groups them:

```python
guid = uuid.uuid4()
fulfill("send_email", 1, subjects, guid=guid)
fulfill("attach_file", 3, subjects, guid=guid)

unfulfill(guid)  # both, as a unit
```

This is what makes "undo the whole operation" a single call rather than bookkeeping you have to do yourself.

## Modifier

A time-limited, subject-scoped factor that changes what a feature costs — a discount, a promotion, a penalty rate. Where several apply, the subject gets the most favourable one.

```python
LicenseModifier.objects.create(
    subject_type="auth.user", subject_id=str(user.pk),
    feature="send_email", factor=Decimal("0.5"),
    valid_until=end_of_promo,
)
```

Modifiers are state, not definition — they belong to a subject and have a validity window, which is exactly why they're rows rather than settings.
