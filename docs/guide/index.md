# What it is

Applications keep needing to answer the same shape of question:

> Can this user send an email right now? How many API calls does this organisation have left this month? Does this device still have a service contract, and does the customer have enough coolant credit to fill it?

`allowances` answers that question, records what the answer cost, and lets you take it back if the operation it paid for never happened.

## What it is not

It is **not a payment system**. There are no invoices, no card charges, no Stripe integration, and no money in it anywhere. The units it counts are whatever you say they are — emails, API calls, litres of coolant, seats — and how they were paid for in the real world is your application's business.

It is **not a feature-flag service**. Flags are a deployment concern and change for everyone at once. Entitlements are per-subject state that gets spent.

It is **not an authorisation framework**. Use it for rights that are bought, expire, or get spent; leave "this role may touch this table" where your framework already knows to look for it. That said, the two overlap more than that sentence suggests — see below.

## What it could also be

A permissions layer, if you push it. That is not what it is designed for, and a static role that never changes is cheaper to answer from a role table than from a wallet. But the pieces line up almost accidentally well:

- A grant wallet with no expiry **is** a permission bit.
- A wallet with a `valid_until` is a *temporary* right — something most permission systems cannot express at all.
- A subject is any `(type, id)` pair, so `("club_membership", 17)` gives you per-object rights without a dedicated object-permissions library.
- The [fulfillment order](/guide/fulfillment-order) is a resolution order: check the user's own right, then the team's, then the organisation's — declared as configuration rather than written as an `if` chain.
- A consumable currency turns into a permission you may exercise *n* times, and revoking the organisation's grant revokes it for everyone who inherited it.
- Every check that spends something leaves a ledger entry, which is the audit trail permission systems usually have to bolt on afterwards.

"May this user edit club members?" is a perfectly legitimate `peek` call today. What you would be giving up is the plumbing, not the semantics: the framework's own permission check, its template helper, its admin hooks and its API permission classes all read the framework's own tables, and most of them cache a user's entire permission set on first access where every check here is a wallet lookup.

That gap is the only thing in the way, and closing it is already scoped: `NEXT_STEPS.md` describes a grant-only Django permissions backend — `has_perm` served from grant wallets, with a per-request cache — after which `@permission_required`, `PermissionRequiredMixin`, `ModelAdmin.has_*_permission` and DRF permission classes all keep working unchanged, because every one of them goes through `has_perm`.

## The problems it does solve

**Enforcement scattered everywhere.** Without something like this, quota checks end up as ad-hoc counters, `if user.is_premium` branches, and a nightly job that emails you when a number looks wrong. Here, enforcement is one call with one answer.

**Two kinds of right that have to work together.** Some capabilities are boolean: you have a subscription or you don't. Others are metered: you have 200 emails left. Real features need both — a subscription that *also* caps you at 200 a day — and they need to be resolved in a single flow, not two.

**Paying from several sources.** One send might come out of a dedicated email budget, or general-purpose credits, or a promotional grant, at different conversion rates, in a priority order that is a business decision rather than a technical one. That order should be configuration, not an `if` chain.

**Knowing what happened.** When a customer asks why their credits are gone, "the balance is 0" is not an answer. Every unit consumed leaves a transaction naming the feature and, once attached, the object it paid for. Reversals are recorded rather than applied, so the history stays intact.

**Licences held by things that aren't users.** A right can belong to a user, an organisation, a device, a physical location — anything. One fulfillment call can resolve across several of them at once, in a defined order.

## The shape of it

Two layers, kept deliberately apart:

**Definition** — what rights exist at all. Features, currencies, conversion rates, priority order. This is configuration: a dict in your settings, or something you fetch from a service. It is not per-subject and it is not in the database.

**State** — what a given subject holds and has spent. Wallets and a transaction ledger. This *is* in the database.

The engine sits between them and knows nothing about either's storage. It receives definitions through a [provider](/api/core#definitionprovider) and state through a [store](/backends/), which is why the same code runs against Postgres, SQLite or a dict.

## Where to go next

- [Getting started](/guide/getting-started) — install it and make one fulfillment call.
- [Concepts](/guide/concepts) — subjects, currencies, features, wallets, transactions.
- [How the port works](/backends/) — if you care about the storage-agnostic part.
