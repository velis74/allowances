# NEXT_STEPS.md — allowances

The design of the split into a dependency-free core (`allowances`) and a Django storage backend
(`allowances_django`) is recorded in `DECISIONS.md` entries 20–28.

## Candidates, none blocking

- **A second real backend.** Only the in-memory and Django stores implement the port; neither covers
  network errors, partial failure or serialisation. A Redis or SQLAlchemy backend would test those.
  `docs/backends/redis.md` proposes Redis as a hybrid with a relational store; an implementation would
  verify that design.
- **Admin actions for the engine.** The seven models are registered for inspection; the admin has no
  "peek" button and no "unfulfill this record" action. Needed for a support workflow operated by people.
- **A remote `DefinitionProvider`.** Implementations: `DictProvider`, `SettingsProvider`,
  `PermissionProvider`. Open: a provider that loads definitions from a licence server, with caching
  and a failure policy.
- **Reconciliation helper for downgrades.** `peek` supports the flow in
  `docs/guide/reversals.md#downgrades`; walking the host application's domain objects is
  application-specific. Open: a documented recipe for it.
- **Exact rounding reconciliation.** `DECISIONS.md` #14: the engine clamps to capacity and back-derives
  `qty_covered` rather than solving for an exact relationship between requested quantity and charged
  currency. Needed only when a consumer requires an exact charge per quantity; the current rule
  guarantees only that no wallet is overdrawn.
- **Performance under high wallet counts.** With a list of steps as `fulfillment_order`,
  `_ordered_wallets` issues one `find_wallets` call per step, wallet type and expanded subject (the
  subject and each of its groups); group expansion adds one `memberships_of` call per level. To be
  profiled for subjects with many wallets or deep group trees.
- **Compaction for long-lived windowed wallets.** `sum_transactions` runs on every fulfilment against
  a windowed wallet. With millions of entries per window this sum becomes slow; aged-out entries could
  be moved to the archive tables.

## Consumable decorator

Blocked on four policy decisions that require a view that uses the decorator:

- What counts as success. An exception is detectable; a view returning `403` itself is not — the
  decorator only sees an `HttpResponse`. Candidate rule: a 2xx response consumes.
- Rollback or storno. Inside `atomic`, an exception rolls back the transactions too, which contradicts
  the rule that reversals are recorded, not applied. Calling `unfulfill` instead requires the fulfill
  to run outside the view's transaction.
- Quantity: fixed, or a callable over the request.
- `StreamingHttpResponse` returns before the response body is produced.

Existing mechanics: `fulfill` deducts immediately (no reserve/commit phase, so concurrent requests
cannot double-spend), several calls sharing a `guid` group into one `FulfillmentRecord`,
`unfulfill(guid)` reverses all of them, and `find_unattached_fulfillment_records` finds the ones that
consumed quota and delivered nothing.

The decorator sits on the view, which has the request; the service function takes the `guid` as an
argument. Templates never consume — they check whether a grant is held (cached) and read the
remaining balance.
