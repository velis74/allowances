---
description: 'What allowances is: grants and consumable budgets with a ledger, resolved across users, teams and organisations, and rights served as Django permissions. Not payments or flags.'
---

# What it is

Applications keep needing to answer the same shape of question:

> Can this user send an email right now? How many API calls does this organisation have left this month? Does this device still have a service contract, and does the customer have enough coolant credit to fill it?

`allowances` answers that question, records what the answer cost, and lets you take it back if the operation it paid for never happened.

## What it is not

It is **not a payment system**. There are no invoices, no card charges, no Stripe integration, and no money in it anywhere. The units it counts are whatever you say they are — emails, API calls, litres of coolant, seats — and how they were paid for in the real world is your application's business.

It is **not a feature-flag service**. Flags are a deployment concern and change for everyone at once. Entitlements are per-subject state that gets spent.

It is **not an authorisation framework**. It has no object-level permissions and no deny rules. It does hold rights — granted, expiring, inherited through groups, audited on use — and serves them to Django's permission checks; see [below](#as-a-permissions-layer).

## As a permissions layer

Rights that are held rather than spent are grant wallets too, and the library serves them to Django as permissions — expiring, bought or plan-bound, inherited through nested groups, and each use can be recorded. Any subject can hold a right; `has_perm` answers for users, and other subjects are checked with `peek` / `fulfill`. The [Permissions quick start](/guide/permissions-quick-start) sets it up; [allowances or django.contrib.auth](/guide/permissions#allowances-or-django-contrib-auth) compares it with Django's own permissions.

- A grant wallet at a conversion rate of 0 with a positive balance is a **right**: `peek` checks it, `fulfill` records each use on the ledger at amount 0. Without `valid_until` it is permanent, with one it is *temporary*, and `close_wallet` revokes it.
- A subject is any `(type, id)` pair, and a subject may be a **member** of other subjects, nested to any depth. A user holds every right of every group it belongs to; a membership that would form a cycle is rejected.
- The [fulfillment order](/guide/fulfillment-order) is a resolution order: the user's own right, then the team's, then the organisation's — declared as configuration rather than written as an `if` chain.
- A ledger entry names its **actor**, so a use paid from a group's wallet still shows which member made it.
- `effective_allowances(subject)` lists every right and balance a subject holds, with the subject or group it comes from; `effective_permissions` picks the Django permissions out of that list.
- `allowances_django.permissions.AllowancesBackend` answers Django's `has_perm` from grant wallets, so `@permission_required`, `PermissionRequiredMixin`, `ModelAdmin.has_*_permission`, `{% if perms.app.codename %}` and DRF permission classes read them unchanged. It works alone or after `ModelBackend`, and caches a user's permission set on the user object.

Its limits: no object-level permissions, no deny rules, and no admin UI for granting — `grant_permission`, `revoke_permission` and `add_member` are the API. See [Rights and permissions](/guide/permissions).

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
- [Permissions quick start](/guide/permissions-quick-start) — Django permissions answered from allowances.
