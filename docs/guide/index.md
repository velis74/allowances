---
description: 'What allowances is: grants and consumable budgets with a ledger, resolved across users, teams and organisations, and rights served as Django permissions. Not payments or flags.'
---

# What it is

`allowances` checks and records rights and budgets held by users, teams, organisations and other subjects. Typical
checks:

- whether a user may send an email now;
- how many API calls an organisation has left this month;
- whether a device has a valid service contract and the customer enough coolant credit to fill it.

`fulfill` checks and consumes the balance and writes a ledger entry; `unfulfill` reverses the entries of an operation
that did not complete.

## What it is not

It is **not a payment system**. It has no invoices, card charges or payment-provider integration. Units are defined by
the application (emails, API calls, litres of coolant, seats); payment for them is outside the library.

It is **not a feature-flag service**. Flags are a deployment setting and change for every subject at once.
Entitlements are per-subject state that is consumed.

It is **not an authorisation framework**. It has no object-level permissions and no deny rules. It holds rights
(granted, expiring, inherited through groups, recorded on use) and serves them to Django's permission checks; see
[below](#as-a-permissions-layer).

## As a permissions layer

Rights that are held rather than consumed are grant wallets as well, and the library serves them to Django as
permissions: expiring, bought or plan-bound, inherited through nested groups, with each use recordable. Any subject can
hold a right; `has_perm` answers for users, and other subjects are checked with `peek` / `fulfill`. The
[Permissions quick start](/guide/permissions-quick-start) sets it up;
[allowances or django.contrib.auth](/guide/permissions#allowances-or-django-contrib-auth) compares it with Django's own
permissions.

- A grant wallet at a conversion rate of 0 with a positive balance is a **right**: `peek` checks it, `fulfill` records
  each use on the ledger at amount 0. Without `valid_until` it is permanent, with one it is *temporary*, and
  `close_wallet` revokes it.
- A subject is any `(type, id)` pair, and a subject may be a **member** of other subjects, nested to any depth. A user
  holds every right of every group it belongs to; a membership that would form a cycle is rejected.
- The [fulfilment order](/guide/fulfillment-order) is a resolution order (the user's own right, then the team's, then
  the organisation's), declared in the feature's `fulfillment_order`.
- A ledger entry names its **actor**, so a use paid from a group's wallet shows which member made it.
- `effective_allowances(subject)` lists every right and balance a subject holds, with the subject or group it comes
  from; `effective_permissions` selects the Django permissions from that list.
- `allowances_django.permissions.AllowancesBackend` answers Django's `has_perm` from grant wallets, so
  `@permission_required`, `PermissionRequiredMixin`, `ModelAdmin.has_*_permission`, `{% if perms.app.codename %}` and
  DRF permission classes use them unchanged. It works alone or after `ModelBackend`, and caches a user's permission set
  on the user object.

Its limits: no object-level permissions, no deny rules, and no admin UI for granting; `grant_permission`,
`revoke_permission` and `add_member` are the API. See [Rights and permissions](/guide/permissions).

## Problems it solves

**One enforcement point.** A quota or entitlement check is one call (`peek` or `fulfill`) that returns one result.

**Grants and consumables together.** Grants are boolean (a subscription); consumables are metered (200 emails). A
feature may require both, for example a subscription with a limit of 200 a day, and one `fulfill` call resolves both.

**Several sources.** One send can draw from a dedicated email budget, general-purpose credits or a promotional grant,
at different conversion rates, in a priority order declared in configuration.

**Audit trail.** Every consumed unit leaves a transaction naming the feature and, once attached, the object it paid
for. Reversals are written as new storno entries, so the history is kept.

**Subjects other than users.** A right can belong to any subject: a user, an organisation, a device, a location. One
fulfilment call resolves several of them, in a defined order.

## Architecture

The library separates two layers:

**Definition**: which rights exist, i.e. features, currencies, conversion rates and priority order. It is
configuration, the same for every subject, read through a [`DefinitionProvider`](/api/core#definitionprovider) (a
settings dict with Django, or another source such as a service).

**State**: what a given subject holds and has consumed, i.e. wallets, the transaction ledger and memberships, kept in
the [store](/backends/).

The engine does not depend on either layer's storage; it runs unchanged on Postgres, SQLite or `MemoryStore`.

## Where to go next

- [Getting started](/guide/getting-started) — install it and make one fulfilment call.
- [Concepts](/guide/concepts) — subjects, currencies, features, wallets, transactions.
- [How the port works](/backends/) — the store interface and backends.
- [Permissions quick start](/guide/permissions-quick-start) — Django permissions answered from allowances.
