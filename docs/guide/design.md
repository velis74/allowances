---
description: 'What allowances is for, what it leaves to the application, and the main design decisions with links to the decision log.'
---

# Scope and design

## What it is for

An application that sells or grants limited use of its features checks the same things in many places: whether a
subject may use a feature, whether it can afford it, which budget pays, and what the use cost. Without a shared
component each feature gets its own counter, its own reset job and its own idea of a refund.

`allowances` puts those checks behind one call. `peek` answers whether an operation is covered, `fulfill` charges it
and writes a ledger entry, `unfulfill` reverses it. Grants, consumable budgets, conversion between currencies,
sliding-window quotas, overdraft and inherited group rights are resolved by the same engine, and every charge is
recorded on one ledger.

## What it does not do

- **Payments.** The library has no invoices, card charges or payment-provider integration. Units are abstract;
  mapping a cost to a price or a payment provider belongs to the application.
- **Feature flags.** A flag changes behaviour for every subject at once; the library holds per-subject state.
- **Object-level authorisation.** It has no per-object permissions and no deny rules.
- **User interface.** It has no UI for granting rights, funding wallets or showing balances. `grant_permission`,
  `credit`, `plan_switch` and `add_member` are the API for changing state.

The consuming application provides:

- the feature and currency definitions, as a settings dict or through its own `DefinitionProvider`;
- the decision when to grant, fund, switch or revoke, and the calls that do it;
- the UI that shows wallets, balances and the ledger to its users;
- the mapping of costs to real prices and payment providers.

## Design decisions

Each decision is recorded with its reasons in
[DECISIONS.md](https://github.com/velis74/allowances/blob/main/DECISIONS.md).

- **Dependency-free core with a storage port.** The engine imports only the standard library and reaches storage
  through the `Store` interface; Django is one backend
  ([21](https://github.com/velis74/allowances/blob/main/DECISIONS.md#21-one-distribution-with-an-optional-extra-not-two-distributions),
  [28](https://github.com/velis74/allowances/blob/main/DECISIONS.md#28-archival-is-a-store-responsibility-the-core-never-names-an-archive-type)).
- **Definitions in configuration, state in the store.** Features and currencies are names read from a provider;
  wallets and transactions are rows
  ([2](https://github.com/velis74/allowances/blob/main/DECISIONS.md#2-walletcurrency-is-a-plain-charfield-not-a-fk-to-a-db-model)).
- **Append-only ledger with storno.** A reversal is a new storno entry; no ledger entry is edited
  ([4](https://github.com/velis74/allowances/blob/main/DECISIONS.md#4-transactionkind-choices-grant-consumption-storno),
  [16](https://github.com/velis74/allowances/blob/main/DECISIONS.md#16-transactionstorno_of-is-on_deleteprotect-so-archiving-deletes-storno-rows-first)).
- **Sliding windows without reset jobs.** A windowed balance is the allotment plus the ledger entries inside the
  current window
  ([13](https://github.com/velis74/allowances/blob/main/DECISIONS.md#13-sliding-window-reset_config-falsy-means-no-sliding-window-json-keys-for-the-four-table-rows)).
- **One global lock order.** Every operation locks all the wallets it uses in one call, in ascending id order
  ([37](https://github.com/velis74/allowances/blob/main/DECISIONS.md#37-one-global-lock-order-every-wallet-an-operation-uses-locked-in-one-lock_wallets-call)).
- **Membership expansion.** A subject draws on the wallets of every group it belongs to, after its own; cycles are
  rejected on write
  ([39](https://github.com/velis74/allowances/blob/main/DECISIONS.md#39-membership-expands-under-the-callers-key-cycles-are-rejected-under-one-lock)).
- **Permissions as rate-0 grants.** A right is a grant wallet at conversion rate 0; each use is a zero-amount ledger
  entry, and Django permissions are `perm:` features held this way
  ([35](https://github.com/velis74/allowances/blob/main/DECISIONS.md#35-a-rate-0-grant-is-a-right-and-each-use-is-a-zero-amount-consumption-entry),
  [40](https://github.com/velis74/allowances/blob/main/DECISIONS.md#40-django-permissions-are-perm-features-served-by-a-provider-held-by-grant-wallets)).
