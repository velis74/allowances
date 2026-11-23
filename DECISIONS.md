# DECISIONS.md — allowances

Decisions made while implementing **Concern 1 (DB schema)**. Each entry: dilemma, decision, reason, and how expensive
it would be to revisit later.

## 1. Decimal precision for balances/amounts: `max_digits=20, decimal_places=6`

**Dilemma:** README examples (`0.5`, `3.27`) only need 2 decimal places; it never states a
required precision.

**Decision:** `max_digits=20, decimal_places=6` for every ledger-adjacent Decimal field
(`Wallet.balance`, `Wallet.overdraft_limit`, `Transaction.amount`, `WalletArchive.final_balance`,
`TransactionArchive.amount`).

**Reason:** 6 decimal places leave room for currencies with finer-grained conversion rates than the README's
examples (e.g. a per-second or per-byte consumable) without a migration to widen the field. Widening
`decimal_places` on a populated ledger table needs a data migration. 20 total digits leave 14 integer digits.

**Cost to revisit:** Low before production (no data), but non-trivial once wallets carry real
balances — changing `decimal_places` on a populated column needs a data migration, not just a
schema migration.

## 2. `Wallet.currency` is a plain `CharField`, not a FK to a DB model

**Dilemma:** README's "Definition vs. State" table explicitly puts currencies in the definition
layer (`settings.py`), but doesn't spell out how `Wallet.currency` should be typed.

**Decision:** `CharField(max_length=100)`, no FK, no DB-level validation against
`settings.ALLOWANCES['currencies']`.

**Reason:** Direct application of the README's "Definition vs. State" principle — a currency
is not a row in this app's tables, it's a name defined in the host application's settings (and,
per "Out of Scope", eventually via a remote `DefinitionProvider`). A FK would tie wallet rows to a
currency table, which the definition/state split excludes. Validating that a wallet's currency name is
one the current provider recognises belongs to the fulfilment engine (concern 2), not to the schema.

**Cost to revisit:** Very low; no change expected.

## 3. `Wallet.wallet_type` (GRANT / CONSUMABLE) is an explicit field

**Dilemma:** The README's DB Models list doesn't mention a "type" field on `Wallet` at all. But
`fulfillment_order` examples walk `(type, subject)` pairs — `("grant", "device")`, `("grant",
None)`, bare `"location"` (shorthand for `(None, "location")`) — which only makes sense if a
wallet itself records whether it fulfils a feature's `grant` or `consumable` section.

**Decision:** Added `Wallet.wallet_type = CharField(choices=["grant", "consumable"])`.

**Reason:** Without it, the engine (concern 2) cannot select "the grant wallet held by this device".
`fulfillment_order` filters wallets by type as well as by subject and currency.

**Cost to revisit:** Medium: wallet selection in the engine depends on this field. Deriving wallet
type from the currency section a wallet holds instead of storing it is a schema change plus a data
backfill.

## 4. `Transaction.kind` choices: `GRANT`, `CONSUMPTION`, `STORNO`

**Dilemma:** README's "Transaction" section says "recorded as a transaction (grant, consumption,
or reversal)" and separately calls reversals "storno transactions" — two different words for the
same third kind.

**Decision:** Used `storno` (not `reversal`) as the third choice value, matching the field name
`storno_of` that the README uses explicitly.

**Reason:** Consistency with the one concrete identifier the README does give
(`storno_of`).

**Cost to revisit:** Low — it's a `TextChoices` value, a rename is a one-line migration plus a
data migration if any rows exist.

## 5. `Transaction.fulfillment_record` is nullable

**Dilemma:** Every transaction the engine creates via `fulfill()` belongs to exactly one
`FulfillmentRecord` (the guid groups them). Should the FK therefore be required?

**Decision:** `null=True, blank=True`.

**Reason:** Keeping it nullable leaves room for ledger entries that do not originate from a
`fulfill()` call — e.g. a manual admin correction, or a data migration seeding initial balances —
without forcing the caller to synthesise a throwaway `FulfillmentRecord` to satisfy a NOT NULL
constraint. The engine (concern 2) always sets it on transactions it creates.

**Cost to revisit:** Low — tightening to NOT NULL later is possible once it's confirmed no
legitimate transaction path needs to omit it.

## 6. Archive tables have no FK back to the live `Wallet`/`Transaction` tables

**Dilemma:** README says "Fully consumed wallets are archived rather than deleted" but doesn't
describe the archive tables' shape at all.

**Decision:** `WalletArchive`/`TransactionArchive` duplicate the relevant live-table fields as
plain columns (`original_wallet_id`, `original_transaction_id` as un-enforced
`PositiveBigIntegerField`s) instead of FKs to `Wallet`/`Transaction`. `TransactionArchive` *does*
FK to `WalletArchive` (both rows are expected to be written together at archival time) and to the
live `FulfillmentRecord` (which is never archived/deleted in this design, so referencing it stays
valid). The subject `GenericForeignKey` on `WalletArchive` still uses a real FK to `ContentType`,
since the subject itself (a User, a Device, ...) is not what's being archived and normally
continues to exist.

**Reason:** Archiving lets the live tables shrink; a live FK from an archive row back to a live row
would either dangle once the live row is deleted (breaking referential integrity) or force
`on_delete=CASCADE`, which would delete archive history whenever a live wallet is deleted. Audit/log
tables commonly use this shape.

**Cost to revisit:** Low — archive tables are additive/audit-only by nature; changing their shape
never risks live data since nothing else needs to be able to join through them.

## 7. `LicenseModifier.factor` precision: `max_digits=10, decimal_places=6`

**Dilemma:** README examples (`0.5`, factor implicit in `("credits", 3.27)` conversion rates) only
need 2 decimal places and are always well under 10.

**Decision:** Smaller precision than the ledger fields (`10/6` vs `20/6`) since a factor is a
multiplier, not an accumulating balance — it never needs the wide integer part a balance might.

**Reason:** A cost-adjustment factor stays within a few orders of magnitude of 1.0, so it does not
need a balance's 14 integer digits.

**Cost to revisit:** Low.

## 8. Python/Django app naming: package `allowances_django`, app label `allowances_django`

**Dilemma:** The library needs a clear Python import path and Django `INSTALLED_APPS` label.

**Decision:** Import package and app label both `allowances_django`, single `apps.py` with
`AllowancesDjangoConfig`, models in one `models.py` (not a `models/` package) since concern 1 has
only six models and splitting them would add indirection without benefit.

**Reason:** Follows the common convention for `django-*` packages (pip name has dashes, import
path has underscores) and keeps the integration point predictable: `INSTALLED_APPS
+= ["allowances_django"]`.

**Cost to revisit:** Very low pre-1.0, but a rename after publishing to PyPI is a breaking change for
every consumer.

## 9. Test database: Postgres, not SQLite

**Dilemma:** README doesn't mandate a test database engine.

**Decision:** Postgres (`tests/env.py`, gitignored; template in `tests/env.example.py`), using a
local Postgres role, but two dedicated databases (`allowances`, `test_allowances`) — never the host database itself.

**Reason:** Production Django deployments typically run on Postgres (as is implied by this library's
`JSONField`/`Decimal` usage), and SQLite's looser type affinity and different `JSONField` backing can
mask precision/constraint bugs (e.g. the `UniqueConstraint` with a `condition=` on
`Transaction.storno_of` relies on partial-index support, which SQLite via Django does support but is
less widely used than on Postgres). Tests run on the production engine, so engine differences show up
in the suite.

**Cost to revisit:** Low — swapping to SQLite later (e.g. for a faster CI matrix) is a
`tests/settings.py` change only, no model changes expected.

> **Superseded by [30](#_30-sqlite-is-a-supported-engine).** SQLite is a supported engine, the
> default for a fresh checkout, and covered by its own CI rows; `tests/env.py` selects Postgres
> rather than being required for the suite to run at all. The concern about partial indexes
> recorded above was unfounded — Django reports `supports_partial_indexes = True` on SQLite, and
> has since 3.8.0.

---

Decisions made while implementing **Concern 2 (fulfilment engine)**. Same rules as above: dilemma, decision, reason,
cost to revisit.

## 10. `Wallet.wallet_type` (grant/consumable) changes only draw priority, not *how* consumption works

**Dilemma:** README's Play-Billing-inspired language ("grant" = boolean entitlement, "consumable" =
spendable budget) suggests grant wallets might not decrement at all when drawn from — they'd just
gate a boolean check. But the `fulfillment_order` examples draw from grant wallets *before*
consumable ones for the exact same feature and quantity, and `Wallet.balance`/`overdraft_limit`/
`first_overdraft_at` are shared fields with no per-type branching in the concern-1 schema.

**Decision:** Both wallet types are spent identically by `fulfill()` — same balance arithmetic,
same `Transaction.kind = CONSUMPTION` regardless of which section funded the draw. `wallet_type`
only changes *which* wallets a `fulfillment_order` step selects (i.e., priority/funding-source
tier — "spend free grants before paid consumable budget"), never the mechanics of spending itself.

**Reason:** This is the only reading consistent with the `fulfillment_order` examples, which
walk both types with the same quantity/currency semantics, and it keeps `TransactionKind.GRANT`
meaningful as "a credit/top-up event" (something a host application writes directly when funding a
wallet — see README "Out of Scope" → "Granting wallets... is the responsibility of the
application") as opposed to "a consumption event that happened to come from a grant-type wallet".

**Cost to revisit:** High: `_consume()` relies on it. Making grant wallets non-decrementing boolean
gates needs a separate branch for `WalletType.GRANT` in `_consume()`/`_ordered_wallets()`, not
just a priority-ordering role.

## 11. Several modifiers combine as `max(factors)`

**Dilemma:** README: "A modifier of 0.5 on `send_email` means each send costs half a unit... Where
multiple modifiers apply, the most favourable (`max`) is used." A factor of 0.5 is *cheaper* than
1.0, so the smaller factor could be read as "more favourable" to the subject — which would suggest
`min()`, not `max()`.

**Decision:** Implemented as written: `_modifier_factor()` returns `max(factors)`.

**Reason:** The README parenthesises `(max)` immediately after "most favourable", defining "most
favourable" as `max`, and the engine follows that definition.

**Cost to revisit:** Low — `max(factors)` → `min(factors)` is a one-line change in
`_modifier_factor()`; no schema impact. Under `max()`, a modifier below 1 (a discount) takes
effect only when no other modifier with a larger factor applies to the same request.

## 12. Overdraft: the feature flag gates *whether* a second pass runs, `overdraft_limit` *which* wallets

**Dilemma:** README/NEXT_STEPS say "a second pass is made over wallets with `overdraft: true`" —
but `overdraft` is a *feature*-level boolean in the settings example, and `Wallet` has no boolean
field at all, only a nullable `overdraft_limit` Decimal.

**Decision:** `feature_def.overdraft` (bool) controls whether `fulfill()`/`peek()` attempt a second
pass at all once the first pass leaves `remaining > 0`. Within that second pass, a wallet is
eligible only if its own `overdraft_limit is not None`; capacity in that pass is
`max(balance, 0) + overdraft_limit` (i.e. the wallet can go negative down to `-overdraft_limit`).

**Reason:** This is the only combination that uses every field concern 1 built
(`Wallet.overdraft_limit`, `Wallet.first_overdraft_at`) while also honouring the feature-level
`overdraft` switch from the settings example — a feature can opt out of overdraft entirely even if
some of its wallets carry a limit (e.g. a limit configured for a *different* feature that
also draws from the same wallet).

**Cost to revisit:** Medium — changing which of the two flags is authoritative (or requiring both)
is a behavioural change with no schema migration needed, but would change existing hosts' overdraft
outcomes without notice if not documented.

## 13. Sliding-window `reset_config`: falsy means no sliding window; JSON keys for the four table rows

**Dilemma:** The README's "Reset config options" table lists `None` as one of *four* values
`reset_config` can take, with the meaning "Rolling window (last N seconds/hours/days)" — implying
`reset_config = None` still means sliding-window (rolling variant), not "no sliding window". But
`Wallet.balance` is documented in concern 1 as a plain "checkpoint" field, and nothing in the schema
says how "last N seconds/hours/days" is parameterised when `reset_config` is literally `None` (no
JSON body to carry N).

**Decision:** `Wallet.reset_config` being DB-`None`/falsy means the wallet is **not**
sliding-window at all — `wallet.balance` is the effective balance directly, exactly like every
wallet built and tested in concern 1. Sliding-window mode requires a non-empty `reset_config` dict,
whose shape determines which of the four table rows applies:

- Rolling: `{"window_seconds": <int>}` (defaults to 86400 if the key is omitted but the dict is
  non-empty; a dict without any of the keys below resolves to a 24h rolling window rather than
  raising an error).
- Daily: `{"time": "HH:MM:SS"}`
- Weekly: `{"day_of_week": 0-6, "time": "HH:MM:SS"}` (0 = Monday, matching `datetime.weekday()`)
- Monthly: `{"day_of_month": 1-31, "time": "HH:MM:SS"}` (clamped to the last day of shorter months)

For a sliding-window wallet, `wallet.balance` is redefined as the **per-window allotment**
(constant, never mutated by `fulfill()`/`unfulfill()`); `effective_balance()` computes
`balance + sum(transactions within the current window)` on every call — see `window_start()` in
`allowances/windows.py` and `effective_balance()` in `allowances/engine.py`.

**Reason:** This is the only reading that (a) keeps every existing concern-1 test and the
`Wallet.balance` docstring ("checkpoint... not the effective balance" — only stated for
sliding-window wallets) true without a migration, and (b) gives the rolling-window case a place to
store its window length, which the README never specifies. With "checkpoint = allotment" for
sliding-window wallets (rather than a running total), the per-window reset is computed and needs no
scheduled job to zero the balance, as the README's "computed on-the-fly" requires.

**Cost to revisit:** Medium-high. The README specifies only the four-row table and "computed
on-the-fly". If a consumer wants `reset_config = None` to mean "rolling window with an implicit
default length" instead, `effective_balance()`'s guard condition
(`if not wallet.reset_config: return wallet.balance`) is the single place to change, but every
sliding-window test in `tests/test_engine_sliding_window.py` would need its wallets given an
explicit `reset_config` dict instead of relying on the default-off behaviour.

## 14. Rounding: `qty_covered` is derived back from the rounded cost and clamped, not reconciled exactly

**Dilemma:** README states rounding is per-currency (floor/ceil/round/None) but never says how to
reconcile a rounded wallet-currency debit against the feature-unit quantity being fulfilled — e.g.
if `ceil` rounds a fractional cost up to more currency than the wallet holds, or up to more
than corresponds to the remaining requested quantity.

**Decision:** After rounding, if the rounded cost would exceed the wallet's available capacity, it
is clamped down to exactly that capacity (never overdraws beyond what was already established as
available). The feature-unit quantity credited for that draw (`qty_covered`) is then derived by
dividing the (possibly clamped) rounded cost back by the effective rate, and is itself clamped to
never exceed the remaining requested quantity. In edge cases (e.g. `ceil` on a very small fractional
rate) this can charge a currency unit slightly out of exact proportion to the quantity nominally
granted for that specific wallet's draw.

**Reason:** With rounding, the debit and the quantity covered cannot always match exactly. Clamping
to capacity guarantees that a wallet's balance never goes past its (overdraft-adjusted) floor because
of a rounding artefact. Tests (`test_fulfill_rounding_ceil`/`floor`/`round` in
`tests/test_engine_fulfill.py`) use round numbers that do not hit this edge case, so it is documented
as a known approximation rather than exhaustively tested.

**Cost to revisit:** Low-medium — confined entirely to `_consume()` in `engine.py`; no schema
impact. Not suitable as it stands for currencies that need exact per-unit reconciliation (e.g.
per-cent accounting).

## 15. Archival has two triggers: "drained" inside `fulfill()` and the `archive_expired_wallets()` sweep

**Dilemma:** README says "Fully consumed wallets are archived rather than deleted" but never
defines "fully consumed", and a wallet can become permanently unusable in two different ways that
`fulfill()` can't uniformly observe: (a) its balance is drawn down to zero with no overdraft left,
which `fulfill()` *does* see (it performed that draw); (b) its `valid_until` passes with the
wallet never touched again, which nothing observes unless something polls for it — `fulfill()`'s
per-draw hook never sees a wallet that expires without being fully drawn, since `_validity_q()`
excludes expired wallets from every candidate query.

**Decision:** Two separate mechanisms:

- `_archive_if_drained(wallet)` runs inside `fulfill()`'s commit loop, immediately after a wallet is
  drawn from, for wallets where `reset_config` is falsy (non-sliding-window) and
  `balance <= 0 and overdraft_limit is None` (no possible future draw without a plan change).
  Sliding-window wallets are never archived by this trigger: their balance is restored in the next
  window.
- `archive_expired_wallets(at=None)` is a standalone function a host application calls periodically
  (e.g. Celery beat) to sweep wallets whose `valid_until` has passed, regardless of whether they
  were ever drawn to zero.

**Reason:** Splitting these covers both cases without `fulfill()` scanning unrelated wallets it
never touched on every call (expensive, and out of scope for a function that fulfils one request).
This follows the "recovery flow" pattern the README already expects
(`find_unattached_fulfillment_records()`, README: "A background job may monitor...") — maintenance
sweeps are the host application's job, this library provides the query/action.

**Cost to revisit:** Low — both are additive; further trigger conditions (e.g. archive on wallet
close via `plan_switch`) can be added without touching either existing path.

## 16. `Transaction.storno_of` is `on_delete=PROTECT`, so archiving deletes storno rows first

**Dilemma:** Not a README ambiguity but a bug in archival: Django's `PROTECT` raises
`ProtectedError` whenever *any* row references the one being deleted — even if that referencing row
is *also* being deleted in the same cascade. `wallet.delete()`'s CASCADE-based collection of the
wallet's transactions therefore fails once a wallet has both an original and its storno transaction,
because the storno's `storno_of` FK protects the original from deletion regardless of the storno also
being scheduled for deletion.

**Decision:** `DjangoStore.archive_wallet()` explicitly deletes all storno transactions for the wallet (the
referencing side of `storno_of`) *before* calling `wallet.delete()`, so nothing is left pointing at
an original by the time the cascade runs.

**Reason:** A storno of a storno cannot exist (the one-storno-per-transaction DB constraint from
concern 1, and `unfulfill()`'s own `.exclude(kind=STORNO)` guard, both prevent it), so a single flat
"delete every storno row first" pass is sufficient — no general topological-delete routine is needed.

**Cost to revisit:** Very low — an implementation detail inside `DjangoStore.archive_wallet()`; it needs
revisiting only if multi-level storno chains are introduced.

## 17. `plan_switch` fan-out: every subject gets its own copy of every `WalletSpec`

**Dilemma:** README's Core API signature is `plan_switch(subjects, open_wallets, effective_date=None)`
with `open_wallets: list[WalletSpec]` — but never states whether `subjects` is a single subject
wrapped for API consistency, or plural, nor how a `WalletSpec` (which carries no subject field) maps
onto multiple subjects if so.

**Decision:** `subjects` is a `dict[str, Model]` exactly like `peek`/`fulfill`. Every value in that
dict has ALL of its currently-valid wallets closed (`valid_until = effective_date`), and then
receives its own newly created wallet for every `WalletSpec` in `open_wallets` — i.e. N subjects ×
M specs = N×M new wallets, not M shared wallets co-owned by N subjects.

**Reason:** `Wallet.subject` is a single `GenericForeignKey`, so a wallet is owned by exactly one
subject — the schema cannot express two subjects sharing one new wallet. Giving each subject its own
full copy of the plan is the only interpretation that produces valid `Wallet` rows.

**Cost to revisit:** Low — a caller that switches a single subject's plan at a time (e.g. one
member's individual plan change) does not use the multi-subject fan-out; no migration implied either
way.

## 18. `general` currency flag is parsed and exposed but has no special engine-side behaviour yet

**Dilemma:** README: "General-purpose currencies act as a fallback for any feature that accepts
them" — but every worked example in the README explicitly lists every currency (including
general-purpose ones like `credits`) in each feature's own `currencies` list. No example shows a
feature relying on implicit fallback to a general currency it never listed.

**Decision:** `CurrencyDefinition.general` is parsed from settings and available to callers/future
providers, but `engine.py` does not auto-inject general-purpose currencies into a feature's
accepted-currency list. A feature only draws from currencies it explicitly lists, general-purpose
or not.

**Reason:** Implementing implicit fallback would mean inventing a conversion rate for currencies a
feature never declared a rate for — the README gives no rule for what that rate should be, and every
example shows rates declared explicitly per feature. The engine implements only the mechanism the
README specifies.

**Cost to revisit:** Low — additive; "if a feature accepts no currency explicitly, fall back to all
`general=True` currencies at rate 1" can be added without changing existing behaviour, since the
current behaviour (nothing implicit happens) is a strict subset.

---

Decision made while integrating the library into a schema-per-tenant deployment with `django-tenants`, with
`allowances_django` in its `TENANT_APPS`.

## 19. `ContentType` foreign keys use `db_constraint=False`

**Dilemma (a bug, not an open question in the README):** Every `ContentType` reference in this app
(`SubjectMixin.subject_content_type` — used by `Wallet`/`LicenseModifier` — plus
`Transaction.object_content_type`, `WalletArchive.subject_content_type`,
`TransactionArchive.object_content_type`) was a plain `ForeignKey(ContentType, ...)`, i.e. a real,
DB-enforced constraint. That works for a single-schema host application, but breaks for a
schema-per-tenant one (`django-tenants` and equivalents): `django.contrib.contenttypes` conventionally
stays a single shared table (Django's own recommendation, and this app's `Wallet.subject` needs
exactly one global `ContentType` catalogue regardless of which schema a subject's table lives in),
while a per-tenant host installs `allowances_django` once per tenant schema, because a wallet's
*subject* (a member, a device, ...) is tenant-scoped data. The result: N tenant schemas each get
their own `wallet`/`transaction`/... tables, all with real FKs to the *one* shared `content_type`
table. Postgres creates such cross-schema FKs but refuses to `TRUNCATE` the shared table —
`FeatureNotSupported: cannot truncate a table referenced in a foreign key constraint` — once a second
schema contains an `allowances_django_wallet` table, even an empty one. In such a deployment's test
suite, any `transaction=True` test that flushed the `public` schema (any test of a `SHARED_APPS` model)
failed at teardown once a tenant schema with `allowances_django` migrated into it existed for the
session.

**Decision:** All four `ForeignKey(ContentType, ...)` fields pass `db_constraint=False`. The
relation still works for every ORM purpose this app needs (`Wallet.subject`, `select_related`,
`ContentType.objects.get_for_model()`); what is removed is the Postgres-enforced constraint and,
with it, `on_delete`'s *database*-level enforcement (Django still runs its own `on_delete` logic in
Python on `.delete()`, without a DB-level backstop). New migration:
`0002_alter_licensemodifier_subject_content_type_and_more.py`.

**Reason:** `ContentType` rows are not deleted in normal operation, so the DB-level `on_delete`
enforcement is not needed. The alternative — forcing every schema-per-tenant host to install
`django.contrib.contenttypes` as a per-tenant app so the FK stays same-schema — would mean a separate
`ContentType` catalogue per tenant, which breaks `ContentType.objects.get_for_model()` returning a
stable, cross-schema-comparable id for the same Python class, and contradicts the `django-tenants`
layout, which keeps `contenttypes` shared. `db_constraint=False` is the common convention for
cross-schema references in schema-per-tenant deployments.

**Cost to revisit:** Low for this app's own code and tests — a field-option change plus one
migration; no query or business-logic path relied on a real FK's behaviour. **Medium for any
already-deployed consumer with existing data**: a project that installed this app in a single-schema
layout and relies on `ON DELETE CASCADE`/`SET NULL` running at the database level (e.g.
bulk-deleting rows through raw SQL that bypasses the ORM) would need to re-verify that assumption.

> **Superseded by [22](#_22-subjects-are-two-charfields-not-a-genericforeignkey).** Subjects are two `CharField`s;
> no `ContentType` foreign key exists.

---

## Concern 4 — ORM decoupling

Entries 20 onward were made while splitting the library into a dependency-free core plus a Django
storage backend. Everything above this line describes the initial design; where
the two disagree, these entries win.

## 20. Package name: `allowances`

**Dilemma:** The package needs a name that is precise yet not already taken on PyPI. Both of
the obvious candidates — `licensing` and `billing` — are taken (`licensing` 0.54,
`billing` 0.0.1). `entitlements` is free. "billing" does not fit: the library has no invoices or
payments.

**Decision:** `allowances`. Bare `allowance` (singular) is taken — a real package with six releases,
not a squat — so the plural is used. `allowances-django` and `django-allowances` were free when
checked, for a possible split of the distribution.

**Reason:** "Allowance" in English means *a permitted quantity* (baggage allowance, tax allowance),
which covers both the boolean grant and the consumable budget.

**Cost to revisit:** High once published. Free until then. Renaming again means another table-prefix
migration for every consumer.

## 21. One distribution with an optional extra, not two distributions

**Dilemma:** A "pure core plus adapter" split naturally suggests two packages (`allowances` and
`allowances-django`), which is what most libraries of this shape do.

**Decision:** One distribution, `allowances`, shipping two importable packages — `allowances`
(no dependencies) and `allowances_django` (needs Django) — with Django behind the `[django]` extra.

**Reason:** `pip install allowances` still pulls in nothing, and `import allowances` still imports no
framework. What is avoided is a second `pyproject.toml`, a second release process, and a
version-compatibility matrix between two packages that are always released together. The only cost
is that `allowances_django` is present on disk even when Django is not installed, where importing it
raises `ImportError`.

**Cost to revisit:** Low. Splitting later is a packaging change, not a code change — the module
boundary is already where the distribution boundary would go.

## 22. Subjects are two `CharField`s, not a `GenericForeignKey`

**Dilemma:** The core cannot import Django, so it cannot hold a `ContentType` foreign key. The
question was whether to keep `ContentType` in the Django backend and translate at the boundary, or
drop it from the schema entirely.

**Decision:** Dropped entirely. `subject_type` holds `"app_label.modelname"` and `subject_id` holds
the primary key as text. `allowances_django.subjects` converts model instances to
`SubjectRef` and back.

**Reason:** Further consequences: subjects keyed by UUID or slug work, which the
`PositiveBigIntegerField` object id ruled out. No foreign key to `django_content_type` exists, so
entry 19's `db_constraint=False` and the cross-schema `TRUNCATE` failure no longer apply. A
schema-per-tenant deployment does not depend on where `django_content_type` lives.

Costs: `select_related` cannot follow a subject, and a dangling reference is not caught by the
database. `resolve()` returns `None` for a deleted subject rather than raising, so admin and
reporting can render an orphaned row instead of failing on it.

**Cost to revisit:** High for a consumer with data — it is a full data migration of every wallet,
transaction and modifier row. Zero for a new installation.

## 23. `fulfillment_order` callables receive a list, not a QuerySet

**Dilemma:** A breaking change in the public configuration API. The callable form previously
received a Django `QuerySet` and was expected to call `.order_by()` on it, which is
unimplementable on any non-ORM backend.

**Decision:** It receives `list[Wallet]` — the core's frozen value objects — and returns them in draw
order. It may filter as well as order.

**Reason:** A QuerySet works only with the Django backend. A list works with every store; adapting an
existing callable takes one `sorted()` call. When the change was made, the only implementation was
this repository's test fixture.

A custom ordering is also unit-testable without a database.

**Cost to revisit:** Low. Passing a lazily-evaluated wrapper instead of a list would be a compatible
extension if the eager `list()` ever becomes a performance problem.

## 24. Locking `unfulfill` at the record, not just the rows

**Dilemma:** The pre-split `unfulfill` computed "which transactions are not yet stornoed" as a
subquery *inside* the locking query, so Postgres re-evaluated it after unblocking. The ported version
computes that set in Python from a separate read, which reintroduces a race: two concurrent reversals
could each read the same not-yet-stornoed set and each write a refund.

**Decision:** `unfulfill` takes an exclusive lock on the fulfilment record itself
(`get_record(guid, lock=True)`) before reading its transactions, which serialises concurrent
reversals of the same record.

**Reason:** It preserves the original guarantee using a primitive every backend can implement, rather
than one that only exists in SQL. The unique constraint on `storno_of` remains as the backstop: if
the lock is ever wrong, the second reversal raises an integrity error rather than refunding twice.

**Cost to revisit:** Low — it is one extra locked read per reversal, on a path that is not hot.

## 25. Unknown guids raise instead of doing nothing

**Dilemma:** `attach` and `unfulfill` previously raised `FulfillmentRecord.DoesNotExist` for a guid
that was never fulfilled under. The core cannot raise a Django exception, and the ported code
initially just returned.

**Decision:** A new `RecordNotFoundError`, raised by both. It subclasses `LookupError`, so a
consumer transitioning from the Django exception can catch `LookupError` and cover both.

**Reason:** `unfulfill` is documented as idempotent, meaning "reversing an already-reversed record is
a no-op". An unknown guid indicates a caller error, and raising makes that error visible.

**Cost to revisit:** Low.

## 26. `created_at` is `default=timezone.now`, not `auto_now_add`

**Dilemma:** The engine threads an `at` parameter through every call, but `auto_now_add` ignored it,
so a transaction always got the real wall clock. Sliding-window behaviour was therefore only testable
by waiting, or by patching rows after the fact — which the pre-split test suite did.

**Decision:** Wallets and transactions use `default=timezone.now`, and the store passes `at` through
when given.

**Reason:** `at=` has the same meaning end to end, as the signature implies. It also makes
backfilling history at real timestamps possible, and removes the `backdate()` helper the old tests
needed.

**Cost to revisit:** Low. Trade-off: a caller can write a transaction at any timestamp, and the
store does not validate it.

## 27. The test suite runs every engine test against both backends

**Dilemma:** With two store implementations, a bug in either is invisible until something exercises
it, and duplicating ~1400 lines of tests per backend is not maintainable.

**Decision:** One engine test suite, parametrised over `MemoryStore` and `DjangoStore` via the
`store` fixture. Backend-specific behaviour (reference conversion, shortcuts, database constraints,
archive tables) lives in `tests/django/`.

**Reason:** Any behavioural difference between the two backends fails a test. A third-party backend
can be verified by adding one parameter to that fixture.

Constraint: the `django` parameter must carry `pytest.mark.django_db` via
`pytest.param(..., marks=...)`. Requesting the `db` fixture from inside the fixture *body*
(`request.getfixturevalue("db")`) is too late — pytest-django decides whether to set up a test
database while collecting markers, so database access is not blocked and the writes go to the
**development** database, without any test failing.

**Cost to revisit:** Low.

## 28. Archival is a store responsibility; the core never names an archive type

**Dilemma:** `WalletArchive`/`TransactionArchive` are relational tables. Hoisting them into the core
as value types would impose one archive shape on every backend.

**Decision:** The port has `archive_wallet(wallet)` and nothing else. The core decides *when* to
archive; what an archive is, and whether one exists at all, is entirely the backend's business.

**Reason:** A backend with no meaningful archive (a cache-shaped one) can raise
`UnsupportedOperationError` rather than being forced to fake a shape it does not have. Reading
archives is likewise backend-specific and stays out of the portable API.

**Cost to revisit:** Medium if a cross-backend archive-reading API is ever wanted — it would mean
adding value types and read methods to the port that only some backends can serve.

## 29. Supported Django versions: 5.2 to 6.x, tested in CI

**Dilemma:** The supported Django versions should be verified in CI. Django 5.1 reached end of life
in December 2025.

**Decision:** `Django>=5.2,<7`, with a GitHub Actions matrix that runs the whole suite against
Django 5.2 (Python 3.11), 6.0 (3.12) and 6.1 (3.13), each against a real Postgres service.

**Reason:** The floor is a support policy, not a technical constraint — the adapter's newest API is
`JSONField` (Django 3.1), and `UniqueConstraint(condition=…)` and `select_for_update()` are older
still. The floor is the current LTS; older versions are not tested. The ceiling `<7` covers only
versions in the CI matrix.

`requires-python = ">=3.11"`: Django 6.0 and later require Python 3.12, so an installation on 3.11
resolves to 5.2 whatever the ceiling. The matrix therefore pairs each Django with the oldest Python
that can run it. Python 3.11 can be dropped only once the floor is 6.x.

**Cost to revisit:** Low. Raising the floor is one line here and one row in the matrix; raising the
ceiling needs a passing matrix row for the new version.

## 30. SQLite is a supported engine

**Dilemma:** The README, `DECISIONS.md` §9 and the Django backend page all said: use Postgres,
because `select_for_update` and partial unique constraints are what the concurrency guarantee rests
on and SQLite implements neither. That excludes single-process, embedded and desktop deployments,
and test runs without a database server.

Django reports `supports_partial_indexes = True` on the SQLite backend, and has since SQLite 3.8.0
in 2013, so the `UniqueConstraint(condition=…)` on `Transaction.storno_of` works on SQLite.
`select_for_update` is not supported (`has_select_for_update = False`) and raises
`NotSupportedError`.

**Decision:** Support both engines.

- `_locked()` in `allowances_django/store.py` applies `select_for_update()` when
  `connections[qs.db].features.has_select_for_update`, and returns the queryset untouched
  otherwise. The store does not name a database engine.
- SQLite deployments are asked for `OPTIONS={"transaction_mode": "IMMEDIATE", "timeout": 20,
  "init_command": "PRAGMA journal_mode=WAL;"}`, and a system check — `allowances.W001`,
  `allowances_django/checks.py` — says so at startup when they are missing.
- `tests/django/test_concurrency.py` tests concurrent `fulfill` calls on whichever engine the suite
  is pointed at.
- `tests/settings.py` defaults to a file-backed SQLite, so `uv sync && uv run pytest`
  passes on a clean checkout with no database server anywhere. `tests/env.py` selects Postgres;
  `ALLOWANCES_TEST_DB=sqlite` forces SQLite past it. CI runs both.

**Reason:** The requirement is that no concurrent unit of work reads a balance this one is about to
change. Row locking is one way to meet it. On SQLite it is met by one write lock over the whole
database, held for the transaction. `transaction_mode: "IMMEDIATE"` (Django 5.1+) makes `atomic()`
take it at `BEGIN` instead of on first write, at which point a `fulfill` is serialised against every
other `fulfill` — strictly stronger than locking the rows it touches.

Verified behaviour:

1. **The concurrency test detects missing locking.** With `_locked()` stubbed to return the queryset
   unchanged, both tests fail against Postgres on lost updates — eight callers, ten units, more than
   five granted. The same stubbed store passes against SQLite, because on SQLite `_locked()` *is*
   that stub and the write lock serialises the callers.
2. **Without `IMMEDIATE`, a conflicting write raises an error.** With `DEFERRED` (Django's default),
   a transaction that reads a balance and then writes it after another transaction has committed
   gets `OperationalError: database is locked` on the write — verified against two connections in
   WAL mode. It does not commit the stale value. So `allowances.W001` is a warning about operational
   noise, not about correctness.

SQLite limits decimal range. It stores `DecimalField` as a float and Django rounds back to six
decimal places, so amounts are exact to about fifteen significant digits — up to
`999999999.999999`. Beyond that the low digits are lost without an error (`1234567890.123456`
returns as `1234567890.123460`) until the rounded value no longer fits `max_digits=20` and
`decimal.InvalidOperation` is raised. `tests/django/test_decimal_precision.py` pins the range both
engines agree on. Postgres is preferable for amounts beyond that range and for concurrent writers
across processes, which a single SQLite write lock serialises.

**Cost to revisit:** Low in one direction, high in the other. Dropping back to Postgres-only is
deleting a helper and some rows from the CI matrix. SQLite support is documented and tested in CI;
withdrawing it is a breaking change for anyone who deploys on SQLite.

## 31. Wallets have a `source`, and `plan_switch` only touches `source == "plan"`

**Dilemma:** `plan_switch` closed every wallet a subject held, and it was the only public way to
fund a wallet. Credits a customer bought outside the plan therefore had nowhere to live: a purchased
pack opened through the store would be closed by the next plan change, losing paid-for balance.

**Decision:** `Wallet.source` is a free string, default `"plan"` (`PLAN_SOURCE`). `plan_switch` opens
wallets with that source and closes only those; `open_wallet(..., source=...)` opens any other
source, which no plan switch ever closes. `fulfillment_order` steps take an optional third element,
`source`, so one subject's wallets of one currency can be ordered by origin (plan before budget).
`credit()` adds to an existing wallet as a `TransactionKind.GRANT` entry, attached to the paying
object; with a `guid` it is reversible by `unfulfill`.

**Reason:** A wallet's source determines whether a plan switch closes it. A free string rather than an enum
lets an application name its own origins (`"purchase"`, `"budget"`, `"promo"`) without a library change;
only `"plan"` has meaning to the engine. Existing rows default to `"plan"`, so existing behaviour is unchanged.

**Cost to revisit:** Medium. The field is in both backends' schemas and in the store port
(`create_wallet`, `save_wallet`).

## 32. Per-spec switch policies: `REPLACE`, `ADJUST`, `TOP_UP`

**Dilemma:** Closing and reopening is wrong for two kinds of wallet. A capacity wallet (storage,
seats) holds *limit − usage*; replacing it hands out the full new limit while usage persists, and a
later `unfulfill` of an upload made before the switch credits the closed wallet, so the space is
never freed. Plan credits under commercial terms like "an upgrade adds the difference, used credits
stay used" also need the old wallet kept. Other wallets — monthly quotas that start over, boolean
grants — are right to be replaced. Which applies is a property of the plan item, not of the
library.

**Decision:** `WalletSpec.on_switch` with three values. `REPLACE` (default) is the previous
behaviour. `ADJUST` and `TOP_UP` keep the current plan wallet of the same type and currency and move
its balance by the difference against a stored `Wallet.allotment` (the plan's amount when the wallet
was opened or last adjusted); `TOP_UP` ignores a negative difference. Both record the new amount as
the allotment, so an upgrade after a downgrade adds the difference against the downgraded plan: the
upgrade is paid for again, and whether a switch is paid is the application's concern.
On windowed wallets both set the per-window allotment and keep the window's history. A wallet
without an allotment, or a windowed/non-windowed mismatch, is replaced. Wallets with an allotment are
exempt from drained-wallet archival. `adjust_wallet()` applies `ADJUST` arithmetic outside a plan
switch (a spending limit changed mid-period).

This supersedes the "close every wallet" part of #17 (the per-subject fan-out stands) and resolves
the open question in #10 about where `TransactionKind.GRANT` is written: by `credit()` and by
non-windowed adjustments.

**Reason:** Storing the allotment rather than deriving it from the ledger keeps the store port free
of kind-filtered aggregates. Exempting allotment wallets from drained archival keeps two things
working: an `ADJUST` capacity wallet at zero (storage full) must stay live so that `unfulfill` of a
deletion frees space in it, and an exhausted `TOP_UP` wallet must still be found on upgrade, or the
switch opens a new wallet with the full new amount instead of the difference. Such a wallet stays
live only while its plan item does; a switch without the item or a cancellation closes it and
`archive_expired_wallets` sweeps it, so the live table holds at most one per subject and plan item.

**Constraints:** Within a static `fulfillment_order` step the engine orders wallets soonest-`valid_until`
first (none last, store order for ties), so expiring plan credits are spent before permanent
purchased ones; the store's `(created_at, id)` order remains its contract.

**Cost to revisit:** Medium. Changing the default policy would change every existing caller's plan
switches.

## 33. Postpaid budgets are a recipe, not a feature

**Dilemma:** Overage billing (a monthly allowance, then spend up to a customer-set limit, billed
afterwards) is common. Implementing it fully means invoicing and payment-provider integration, which
the library does not do.

**Decision:** The library supplies the pieces — a `"budget"`-source wallet with an `ADJUST`
allotment as the limit, source-filtered `fulfillment_order` steps, `adjust_wallet()` for limit
changes, and `consumed(wallet, since, until)` (net consumption in a period, reversals of consumption
deducted, credits and adjustments ignored) as the billing figure. `docs/guide/credits-and-budgets.md`
shows the assembly. `consumed` needs `Store.transactions_for_wallet`, added to the port.

**Reason:** Invoicing is the application's responsibility; the library supplies the consumption figure from the
ledger.

**Cost to revisit:** Low. Everything is additive.

## 34. Scheduled plan switches are recorded on the wallets and applied lazily

**Dilemma:** A downgrade or cancellation usually takes effect at the end of the paid period, so a
switch is often decided now and effective later. Closing and opening wallets can be scheduled
through validity dates, but an `ADJUST`/`TOP_UP` adjustment changes a stored balance, which has no
future-dated form. A scheduled switch also has to be cancellable (the customer changes their mind),
and validity dates alone do not say which `valid_until` a switch set and which the wallet already
had.

**Decision:** A switch with a future `effective_date` writes `Wallet.scheduled` on every wallet it
affects: `close` (with the previous `valid_until`), `open`, or `adjust` (target allotment, policy and
spec fields). An adjustment is applied lazily: readers (`peek`, `effective_balance`) see it from its
date on, and the first locked operation reaching the wallet (`fulfill`, `unfulfill`, `credit`,
`adjust_wallet`, `plan_switch`, `archive_expired_wallets`) writes it, its `GRANT` entry dated on the
effective date. The difference is computed at that point, against the allotment the wallet has then.
`cancel_scheduled_switch` reverts not-yet-effective records: previous `valid_until` restored,
adjustments dropped, opened wallets given a `valid_until` before their `valid_from`. `plan_switch`
cancels a subject's pending switch before applying a new one, so there is at most one per subject.
`Store.close_wallets` is replaced by per-wallet `save_wallet` calls, and
`Store.find_scheduled_wallets` finds the records.

**Reason:** Lazy application keeps the property that nothing has to run on the effective date, as
with sliding windows. Recording per wallet rather than in a separate switch table keeps the port
small and the record next to the state it changes.

**Cost to revisit:** Medium. The record format is internal to the engine, but stored; changing it
needs a data migration for pending switches.

## 35. A rate-0 grant is a right, and each use is a zero-amount consumption entry

**Dilemma:** A feature section may accept a currency at rate 0, and the engine's comment said such a
wallet covers the remainder for free. In practice the zero cost hit the "nothing to draw" guard, so a
rate-0 wallet never satisfied `peek` or `fulfill`. Rights (may edit members, may export) need exactly
that shape: held, not spent. A use of a right also has to be auditable, which a check that writes
nothing is not.

**Decision:** A wallet whose effective rate (rate × modifier) is 0 and whose effective balance is
positive covers the whole remaining quantity at cost 0. A balance of 0 or less means the right is not
held; rate-0 wallets never take part in the overdraft pass. `peek` writes nothing; `fulfill` writes a
`CONSUMPTION` entry of amount 0 under its fulfilment record, without saving or archiving the wallet;
`unfulfill` writes `STORNO` entries of amount 0. A permanent right is a `GRANT` wallet at rate 0 with
balance 1 and no `valid_until`; `valid_until` makes it temporary, `close_wallet` revokes it.

**Reason:** No new wallet type or table: a right is a grant that costs nothing, and every existing
mechanism (validity, fulfilment order, membership expansion, attach, unfulfill, the sweep) applies
unchanged. The zero entry puts the use, its actor and its object on the ledger, which is the audit
trail. Because the rule reads the effective rate, a modifier factor of 0 also makes a paid wallet
cover the request for free.

**Cost to revisit:** Low. Zero entries are ordinary rows; dropping them would only remove audit rows.

## 36. Transfer is its own transaction kind, and its reversal needs the target to still hold the amount

**Dilemma:** Funding one wallet from another (a guide's budget handed to tourists) could be written as
a consumption on the source plus a `credit` on the target. That counts the move as consumption in
`consumed()` billing, splits it over two records, and makes the recovery sweep treat it as abandoned
until something is attached.

**Decision:** `transfer(from, to, amount)` writes two `TransactionKind.TRANSFER` entries, debit and
credit, under one fulfilment record. Same currency required, same wallet refused, amount positive,
wallet types may differ. The source must hold the amount (`max(effective_balance, 0)`); overdraft is
never used. `unfulfill` reverses both legs, and refuses with `InsufficientBalanceError`, writing
nothing, when the target holds less than the amount or has been archived. When the source has been
archived since, `unfulfill` raises `WalletNotFoundError` and writes nothing: skipping it, as for an
archived wallet of a consumption, would take the amount from the target and return it nowhere. The sweep
(`records_with_unattached_transactions`) considers only `CONSUMPTION` entries, which also stops it
reporting `credit(guid=...)` without `obj`.
`consumed()` counts a positive storno only when it reverses a consumption.

**Reason:** A transfer is not consumption. Its own kind keeps it out of consumption
figures and the sweep, and one record makes it reversible as a unit. Refusing a reversal the target
cannot cover keeps the source from being refunded an amount the target has already spent; returning
the unspent part is a new transfer in the opposite direction.

**Cost to revisit:** Medium. The kind is stored in ledgers, and the sweep change is a behaviour change
for callers that relied on it for credits.

## 37. One global lock order: every wallet an operation uses, locked in one `lock_wallets` call

**Dilemma:** `fulfill` locked candidate wallets step by step in fulfilment order and `unfulfill` in
transaction order. Two operations over the same wallets in opposite orders (user then team against
team then user) deadlocked on PostgreSQL; `tests/django/test_lock_order.py` reproduces it.

**Decision:** New port method `Store.lock_wallets(wallet_ids)`: locks the given live wallets in
ascending id order and returns them in that order, without archived or unknown ids. An operation finds
its wallets without locks, locks all of them in one call before reading the balances it relies on,
and locks no further wallet afterwards. A fulfilment record is locked before its wallets (`unfulfill`,
and `fulfill`, `credit`, `transfer` given an existing guid). `fulfill`, `unfulfill`, `transfer`,
`close_wallet`, `plan_switch`, `cancel_scheduled_switch` and `archive_expired_wallets` follow it;
`credit` and `adjust_wallet` lock their single wallet. Wallet ids of one store must be mutually
comparable. `DjangoStore` implements it as `select_for_update()` ordered by id.

**Reason:** A single total order over all locks is the standard way to rule out lock cycles, and
ascending primary key is an order every store has. Locking the found candidates once and re-reading
them under the lock keeps scheduled-switch settling and validity checks on fresh state.

**Cost to revisit:** Medium. It is a required port method, and every new multi-wallet operation must
keep the rule.

## 38. The actor defaults to the only subject passed

**Dilemma:** Once a group's wallet can pay for a member's use, the wallet's subject no longer says who
acted. The ledger needs the actor on the entry, but most calls pass a single subject, so the actor
defaults to that subject.

**Decision:** `Transaction.actor: SubjectRef | None`, stored by `create_transaction(actor=)`
(`actor_type` / `actor_id` on the Django models, copied on archival). `fulfill(actor=None)` records the
single subject of `subjects` when it holds exactly one entry, before membership expansion; otherwise
`None`. `credit`, `transfer` and `unfulfill` store the actor as given; storno entries carry the actor
passed to `unfulfill` (who reversed it), not a copy of the original's.

**Reason:** With one subject the actor is unambiguous; with several (user and team) the actor is
ambiguous, so it is recorded only when the caller passes it. Taking the subject before expansion
keeps a group from ever appearing as the actor of a member's use.

**Cost to revisit:** Low. The field is nullable and the default is a single line.

## 39. Membership expands under the caller's key; cycles are rejected under one lock

**Dilemma:** Rights and budgets of a team or organisation should reach its members without every
caller passing every group. Groups may nest, and arbitrary nesting admits cycles, which a resolver
must survive and an API should not create. Two concurrent additions can each pass a cycle check and
together form a cycle.

**Decision:** A group is an ordinary subject. Port methods `add_membership`, `remove_membership`,
`memberships_of` (direct pairs, oldest first) and `lock_memberships`; Django model `Membership`.
`add_member` takes `lock_memberships`, rejects `member == group` or a group already transitively a
member of the member with `MembershipCycleError`, and treats an existing membership as a no-op.
`groups_of` is breadth-first, deduplicated and terminates on cyclic data. `peek` and `fulfill` expand
every caller-supplied subject under its own key into `[subject, depth-1 groups, depth-2 groups, ...]`;
a step naming the key walks that list, a step without a subject walks all expanded subjects, a
callable order and modifier lookup see all of them, and a wallet is drawn at most once. On PostgreSQL
`lock_memberships` is `pg_advisory_xact_lock` on a fixed key; on SQLite the `IMMEDIATE` write lock
already serialises writers.

**Reason:** Expanding under the caller's key keeps every existing `fulfillment_order` meaningful: the
user's own wallets still come first, the groups' after them, and without memberships nothing changes.
Rejecting cycles at write time keeps the data acyclic (a DAG); tolerating them at read time keeps a
corrupted table from hanging a request. An advisory lock serialises only membership writers, not the
whole membership table.

**Cost to revisit:** Medium. Expansion order is observable in which wallet pays; changing it changes
charges.

## 40. Django permissions are `perm:` features served by a provider, held by grant wallets

**Dilemma:** Serving `has_perm` from wallets needs a mapping from `"app_label.codename"` to a feature
and currency, and `get_all_permissions` needs the reverse. A settings dict per permission would
duplicate every permission the project defines.

**Decision:** Naming convention: the feature and the currency of permission `"app_label.codename"` are
both `"perm:app_label.codename"` (`PERMISSION_PREFIX`, `permission_feature`). `PermissionProvider(base)`
delegates to `base` and serves any undefined `perm:` name: a plain currency, and a feature with a grant
section accepting that currency at rate 0 and fulfilment order `[("grant", None)]`. The shortcuts
default to `PermissionProvider(SettingsProvider())`. `grant_permission` opens a balance-1 grant wallet
with source `"permission"`; `revoke_permission` closes the subject's live wallets of that currency.
`AllowancesBackend` reads the grant wallets of the user and of `groups_of(user)` valid now with a
positive balance, maps currencies with the prefix back to permission names, gives an active superuser
every `Permission`, and caches the set on the user object. It does not subclass `BaseBackend`, so
importing the shortcuts does not require `django.contrib.auth`.

**Reason:** The reverse mapping is the prefix itself, so `get_all_permissions` is one wallet query and
no startup index. A `perm:` name a project does define in settings still wins, so a permission can be
given a different order or currency. Grant-only keeps `has_perm` free of side effects: templates and
admin lists call it on every render.

**Cost to revisit:** Medium. The prefix is stored in wallet currencies; changing it needs a data
migration.

## 41. `effective_allowances` is the general read; Django permissions are a filter over it

**Dilemma:** Showing where a user's Django permissions come from needs the holder and depth of every
grant. A permissions-only query would answer that for `perm:` wallets and leave the same question —
what a subject holds, and through which group — unanswered for budgets, credits and other rights.

**Decision:** The core function `effective_allowances(store, subject, ...)` returns every live wallet of
the subject and of `groups_of(subject)` as `Allowance(wallet, holder, depth, balance)`, in one
`find_wallets` call, for any currency and wallet type, whatever the balance, ordered by depth, holder
position and store order. `allowances_django.permissions.effective_permissions(allowances)` is a pure
converter: grant allowances in a `perm:` currency with a positive balance, keyed by permission name.
`AllowancesBackend` builds its permission set as the keys of the converter applied to the user's grant
allowances.

**Reason:** One read in the library's own terms serves budgets, rights and permissions alike, and works
for any subject, not only users. Keeping the Django view as a filter means it cannot disagree with what
the engine sees, and the balance filter stays with the caller, who may want empty wallets listed.

**Cost to revisit:** Low. Both functions are reads; their result types are the public contract.
