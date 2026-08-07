# NEXT_STEPS.md — allowances

**Concern 1** built the DB schema, **concern 2** the fulfillment engine, **concern 3** the docs site.
**Concern 4** split the library into a dependency-free core (`allowances`) plus a Django storage
backend (`allowances_django`).

Read `DECISIONS.md` entries 20–28 before touching the split — several of them (the subject-column
change, the `fulfillment_order` callable contract, the record-level lock in `unfulfill`) are
deliberate design choices, not accidents.

## Open, in rough priority order

- **Publish to PyPI.** `allowances` is reserved by nobody yet — the name was verified free during
  concern 4, and that stays true only until someone else takes it.
- **Hosting for the docs site.** `npm run docs:build` produces a static site in
  `docs/.vitepress/dist`. Nothing publishes it anywhere.

## Candidates, none blocking

- **A second real backend.** The in-memory store proves the port is implementable, but it is not
  adversarial — it has no network, no partial failure, no serialisation. A Redis or SQLAlchemy backend
  would find whatever the port still assumes. `docs/backends/redis.md` argues Redis should be a hybrid
  rather than a replacement; that argument is worth testing against an actual implementation.
- **Admin actions for the engine.** The six models are registered for inspection, but nothing offers a
  "peek" button or an "unfulfill this record" action. Worth adding if this gets a human-operated
  support workflow.
- **A remote `DefinitionProvider`.** The interface is stable and has two implementations
  (`DictProvider`, `SettingsProvider`). A provider fetching definitions from a licence server — with
  caching and a failure policy — is now a self-contained piece of work.
- **Reconciliation helper for downgrades.** `peek` supports the flow described in
  `docs/guide/reversals.md#downgrades`, but nothing walks a host application's domain objects for it.
  Deliberately so — that part is application-specific — though a documented recipe would reduce
  friction for the first consumer that needs it.
- **Exact rounding reconciliation.** `DECISIONS.md` #14: the engine clamps to capacity and back-derives
  `qty_covered` rather than solving for an exact relationship between requested quantity and charged
  currency. Revisit if a consumer needs cent-exact behaviour rather than "never overdraws, close
  enough".
- **Performance under high wallet counts.** `_ordered_wallets` issues one store call per
  `(wallet_type, subject)` step in a static `fulfillment_order`. Fine at the counts expected today;
  worth profiling if a consumer has far more wallets per subject than the test fixtures do.
- **Compaction for long-lived windowed wallets.** `sum_transactions` runs on every fulfillment against
  a windowed wallet. At millions of entries per window that will hurt, and the archive tables are
  already the right place to move aged-out entries to.

## Django permissions backend (grant-only)

Serve `user.has_perm()` from grant wallets instead of `auth_permission`. A grant with no expiry is a
permanent right, one with `valid_until` a temporary one, and any `(type, id)` subject gives per-object
rights — so the overlap with Django permissions is real, not theoretical.

Scope is grants only. Consumables stay out of `has_perm`: it is called from templates and admin lists,
where a side effect would burn quota on every render.

What it takes:

- `AllowancesBackend.has_perm` / `has_module_perms` — `peek` over `WalletType.GRANT`. ~80 lines.
- A `subject_resolver` hook, `(user, obj) -> {"user": u, ...}`, defaulting to `{"user": user}`.
  Django's `has_perm(user, perm, obj)` signature has nowhere to put a subject dict, so the adapter
  has to build one. Core stays agnostic.
- Mapping `"club.change_member"` → feature name: convention or a dict in settings.
- Per-request cache. Django caches a user's whole permission set on first access; every check here
  is a wallet lookup.
- `get_all_permissions()` — the only non-trivial piece. Django wants the *set* of permissions, not an
  answer about one, so this needs a reverse index from grant currencies back to the features that
  accept them. Buildable at startup because definitions are static. Without it,
  `{% if perms.club.change_member %}` does not work.
- `is_superuser` bypass, if `ModelBackend` is dropped rather than chained.

Free once the backend works: admin's `has_*_permission`, DRF permission classes,
`@permission_required`, `PermissionRequiredMixin` — all of them call `has_perm`.

Drops out of scope: the admin UI for assigning permissions (`user_permissions`, `Group`) edits Django
tables directly. Granting rights would need its own UI over wallets.

Estimate: a day or two.

### Consumable decorator

Separate and smaller, but blocked on four policy decisions that only a real view can settle:

- What counts as success. An exception is easy; a view returning `403` itself is not — the decorator
  only sees an `HttpResponse`. Probably "2xx consumes", and that rule will be wrong once.
- Rollback or storno. Inside `atomic`, an exception erases the transactions too — clean, but against
  the "reversals are recorded, not applied" rule. Calling `unfulfill` instead requires the fulfill to
  live outside the view's transaction.
- Quantity: fixed, or a callable over the request.
- `StreamingHttpResponse` returns before anything has happened.

Mechanics are already in place: `fulfill` deducts immediately (no reserve/commit phase, so concurrent
requests cannot double-spend), several calls sharing a `guid` group into one `FulfillmentRecord`,
`unfulfill(guid)` reverses the lot, and `find_unattached_fulfillment_records` finds the ones that
consumed quota and delivered nothing.

The decorator sits on the view, which knows about the request; the service function takes the `guid`
as an argument. Templates never consume — they render "may I" (grant, cached) and "how much is left".

Wait for a view that actually needs it, or the four policies will be guesswork.


## Not this repo, ever

Consumer-specific wiring — feature and currency definitions, wallet UI, cost mapping to real payment
providers — belongs in the consuming application, never here.
