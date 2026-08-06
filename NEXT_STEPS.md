# NEXT_STEPS.md — django-licensing

**Concern 1** implemented the DB schema. **Concern 2** implemented the fulfillment engine
(`django_licensing/engine.py`) and the settings-backed definition provider
(`django_licensing/providers.py`) — see `README.md` "Project Status" and `DECISIONS.md` (entries 10
onward) for exactly what was built and every judgment call made along the way. Read `DECISIONS.md`
entries 10-18 before touching `engine.py` — several of them (modifier `max()` direction, overdraft
eligibility rules, sliding-window `reset_config` key names, the archival trigger split) are this
library's own inventions where the README was silent, not things re-derivable from the README text
alone.

## Concern 3 — candidates (none of these block KlubIS from depending on concerns 1+2 today)

- **Django admin integration for the engine.** Concern 1 registered the six models in
  `admin.py` for basic CRUD/inspection. Nothing in concern 2 added admin actions (e.g. a "peek"
  button, an "unfulfill this FulfillmentRecord" action) — worth adding if this library gets a
  human-operated support/ops workflow.
- **`ServerProvider` skeleton.** README "Out of Scope" defers the actual remote-licence-server
  provider, but a *skeleton* (implementing `DefinitionProvider`, raising `NotImplementedError` or
  similar, with the HTTP/caching shape sketched out) could be scaffolded now that the interface
  (`providers.py::DefinitionProvider`) is stable and has one real implementation
  (`SettingsProvider`) to model it against.
- **Reconciliation helper for downgrades.** README "Out of Scope" → "Reconciliation": "when a
  subject's entitlement decreases... the application is responsible for detecting which domain
  objects exceed the new limit and calling `unfulfill` for them. The library provides `peek` to
  support this flow." `peek()` exists; no higher-level helper walks a host application's domain
  objects for it (deliberately — that's app-specific), but a documented recipe/example in
  `docs-technical`-equivalent (this repo doesn't have that dir yet — KlubIS does) could reduce
  friction for the first real consumer.
- **Exact rounding reconciliation.** DECISIONS.md #14 — current behaviour clamps to capacity and
  back-derives `qty_covered` rather than solving for an exact "requested quantity relates precisely
  to charged currency" reconciliation. Revisit if a KlubIS use case (e.g. exact monetary billing)
  needs cent-exact behaviour rather than "never overdraws, close enough".
- **`fulfillment_order` callable authoring ergonomics.** The dotted-string callable mechanism
  works (`tests/fixtures_ordering.py` is a working example), but there's no documented contract
  beyond "takes a QuerySet, returns something iterable of Wallets in draw order" — e.g. should it
  support returning a list instead of a QuerySet (it can, `list()` is called on the result either
  way), can it filter as well as order, does the first/second overdraft pass see the *same*
  callable-produced order both times (yes — see `_build_ordered_wallets`/`_consume` in
  `engine.py`)? Worth a short "authoring guide" once a second real callable (beyond the test
  fixture) exists to generalise from.
- **Bulk/performance path for `fulfill()` under high wallet counts.** The current
  `_build_ordered_wallets()` walk issues one query per `(wallet_type, subject)` combination in a
  static `fulfillment_order` list (not per wallet) — fine for the wallet counts KlubIS expects
  (poglavje 12: "real-time... klubi so dovolj majhni"), but worth profiling if a future consumer has
  many more wallets/subjects per fulfillment call than this library's own test fixtures.

## Not concern 3 either (later still)

- Remote `ServerProvider` *implementation* (only a skeleton is even a concern-3 candidate above;
  the real thing talks to a service that doesn't exist yet).
- Any KlubIS-specific wiring (feature/currency definitions, wallet UI, Stripe/SMS cost mapping) —
  that all belongs in KlubIS's own `setup` module, never in this repo (see this library's
  `README.md` "Project Status" note and `klubis-novi`'s `CLAUDE.md` poglavje 3/4).
