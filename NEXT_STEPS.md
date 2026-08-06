# NEXT_STEPS.md — django-licensing

**Concern 1 (this repo's current state)** implemented only the DB schema — see `README.md`
"Project Status" and `DECISIONS.md` for what was decided along the way. Nothing below is
implemented yet.

## Concern 2 — the fulfillment engine

The next concern is the actual business logic sitting on top of the schema built here. In README
terms (`README.md` — read "Core Concepts" and "Core API" in full before starting):

- **`settings.LICENSING` provider.** A provider interface (README: "Definition vs. State" /
  "Out of Scope" → future `ServerProvider`) that reads `Feature`/`Currency` definitions from
  Django settings — parsing the `LICENSING` dict shape shown in the "Feature" section example,
  including the `("credits", 0.5)` currency-with-conversion-rate tuple form, and the
  `general`/`round_fn` currency attributes.
- **`fulfillment_order` walk.** Implement the ordered walk described in "Fulfillment Order":
  `(type, subject)` tuples with `None` wildcards, the shorthand `"location"` ==
  `(None, "location")` form, a first pass over non-overdraft wallets and a second pass over
  `overdraft: true` wallets only if the first pass didn't satisfy the requested quantity. Also
  support the dotted-string-callable form (`"myapp.licensing.my_ordering_fn"`) that receives and
  reorders a QuerySet of candidate wallets.
- **`peek(feature, quantity, subjects) → FulfillmentResult`** — read-only availability check, no
  writes.
- **`fulfill(feature, quantity, subjects, guid=None, allow_partial=False) → FulfillmentResult`** —
  the actual consumption path: creates/reuses a `FulfillmentRecord` for `guid`, draws from wallets
  per the fulfillment order, creates `Transaction` rows, calls `Wallet.mark_overdraft_if_needed()`
  (already implemented as a model method in concern 1 — this is where it actually gets called) at
  the right point, and applies `LicenseModifier` factors (most favourable — `max` — wins when
  several apply) with per-currency rounding (`floor`/`ceil`/`round`/`None`, from `round_fn`).
- **`attach(guid, obj) → None`** — sets `object_content_type`/`object_id` on the transactions for
  a `FulfillmentRecord`, FIFO by timestamp, per "FulfillmentRecord": "written in two stages".
- **`unfulfill(guid) → None`** — creates storno `Transaction` rows (`storno_of` set, respecting
  the one-storno-per-transaction constraint already enforced at the DB level in concern 1) that
  reverse everything under a `FulfillmentRecord`.
- **`plan_switch(subjects, open_wallets, effective_date=None) → None`** — per "Core API": sets
  `valid_until = effective_date` on all currently active wallets for the given subjects and opens
  new ones from `open_wallets` with `valid_from = effective_date`, atomically. Immediate switch
  when `effective_date` is `None`.
- **Sliding-window balance computation.** Per "Wallet": "The effective balance of a sliding-window
  wallet is computed on-the-fly from its transactions within the current window" — this is
  distinct from the stored `Wallet.balance` checkpoint field already in the schema. Needs to
  interpret the `reset_config` shapes from the README's "Reset config options" table (rolling
  window / daily / weekly / monthly), all in UTC.
- **Archival trigger.** Decide *when* a wallet becomes "fully consumed" enough to move it (and its
  transactions) into `WalletArchive`/`TransactionArchive` (schema for both already exists — see
  `DECISIONS.md` #6 for why they don't FK back to the live tables), and implement the copy step.
- **`FulfillmentResult`** dataclass/NamedTuple: `requested`, `granted`, `satisfied`,
  `transactions`, `guid`, `modifier_applied` — exact shape given in README "Core API".
- **Recovery-flow support.** README: "A background job may monitor `FulfillmentRecord`s with
  unattached transactions to trigger recovery flows" — not this library's job to run the
  background job itself, but consider what query/manager method makes "find unattached
  fulfillments older than X" easy for a host application to build.

## Not concern 2 either (later still)

- Remote `ServerProvider` definition source (README: "Out of Scope" explicitly defers this).
- Any KlubIS-specific wiring (feature/currency definitions, wallet UI, Stripe/SMS cost mapping) —
  that all belongs in KlubIS's own `setup` module, never in this repo (see this library's
  `README.md` "Project Status" note and `klubis-novi`'s `CLAUDE.md` poglavje 3/4).
