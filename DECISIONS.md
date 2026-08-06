# DECISIONS.md — django-licensing

Autonomous-session decisions made while implementing **Concern 1 (DB schema)**, where the README
(`/home/jure/Nextcloud/README_django_lic_lib.md`, copied into this repo as `README.md`) is silent
or ambiguous about exact field types/precision/nullability. Each entry: dilemma, decision, reason,
and how expensive it would be to revisit later.

## 1. Decimal precision for balances/amounts: `max_digits=20, decimal_places=6`

**Dilemma:** README examples (`0.5`, `3.27`) only need 2 decimal places; it never states a
required precision.

**Decision:** `max_digits=20, decimal_places=6` for every ledger-adjacent Decimal field
(`Wallet.balance`, `Wallet.overdraft_limit`, `Transaction.amount`, `WalletArchive.final_balance`,
`TransactionArchive.amount`).

**Reason:** 6 decimal places gives headroom for currencies with finer-grained conversion rates
than the README's own examples (e.g. a future per-second or per-byte consumable) without ever
needing a migration to widen the field — widening `decimal_places` on a live ledger table is the
kind of migration you want to avoid needing. 20 total digits leaves 14 integer digits, comfortably
above any realistic wallet balance.

**Cost to revisit:** Low now (pre-production, no data), but non-trivial once wallets carry real
balances — changing `decimal_places` on a populated column needs a data migration, not just a
schema migration. Flagged here specifically so it gets attention before the engine (concern 2)
starts writing real values.

## 2. `Wallet.currency` is a plain `CharField`, not a FK to a DB model

**Dilemma:** README's "Definition vs. State" table explicitly puts currencies in the definition
layer (`settings.py`), but doesn't spell out how `Wallet.currency` should be typed.

**Decision:** `CharField(max_length=100)`, no FK, no DB-level validation against
`settings.LICENSING['currencies']`.

**Reason:** Direct application of the README's own "Definition vs. State" principle — a currency
is not a row in this app's tables, it's a name defined in the host application's settings (and,
per "Out of Scope", eventually via a remote `DefinitionProvider`). A FK would resurrect exactly
the coupling the README is designed to avoid. Validating that a wallet's currency name is one the
current provider recognises is fulfillment-engine business (concern 2), not schema business.

**Cost to revisit:** Very low — this is exactly the shape the README calls for; no expected
change.

## 3. `Wallet.wallet_type` (GRANT / CONSUMABLE) is an explicit field

**Dilemma:** The README's DB Models list doesn't mention a "type" field on `Wallet` at all. But
`fulfillment_order` examples walk `(type, subject)` pairs — `("grant", "device")`, `("grant",
None)`, bare `"location"` (shorthand for `(None, "location")`) — which only makes sense if a
wallet itself records whether it fulfils a feature's `grant` or `consumable` section.

**Decision:** Added `Wallet.wallet_type = CharField(choices=["grant", "consumable"])`.

**Reason:** Without it, the engine (concern 2) would have no way to answer "give me the grant
wallet held by this device" — the entire `fulfillment_order` mechanism described in the README
depends on being able to filter wallets by this axis, in addition to subject and currency.

**Cost to revisit:** Medium — this is load-bearing for the engine's core walk logic. If a future
session decides wallet type should instead be derived from which currency section it holds
(rather than stored explicitly), that's a schema change plus a data backfill.

## 4. `Transaction.kind` choices: `GRANT`, `CONSUMPTION`, `STORNO`

**Dilemma:** README's "Transaction" section says "recorded as a transaction (grant, consumption,
or reversal)" and separately calls reversals "storno transactions" — two different words for the
same third kind.

**Decision:** Used `storno` (not `reversal`) as the third choice value, matching the field name
`storno_of` that the README uses explicitly, and matching the KlubIS billing module's own
established vocabulary (`Storno` / `CANCELLED`) for the same concept in that project.

**Reason:** Consistency with the one concrete identifier the README does give
(`storno_of`), and with the sibling KlubIS codebase this library will be consumed by.

**Cost to revisit:** Low — it's a `TextChoices` value, a rename is a one-line migration plus a
data migration if any rows exist.

## 5. `Transaction.fulfillment_record` is nullable

**Dilemma:** Every transaction the engine creates via `fulfill()` will belong to exactly one
`FulfillmentRecord` (that's the whole point of the guid grouping). Should the FK therefore be
required?

**Decision:** `null=True, blank=True`.

**Reason:** Keeping it nullable leaves room for ledger entries that legitimately don't originate
from a `fulfill()` call — e.g. a manual admin correction, or a data migration seeding initial
balances — without forcing the caller to synthesize a throwaway `FulfillmentRecord` just to
satisfy a NOT NULL constraint. The engine (concern 2) is expected to always set it when it creates
transactions itself; this is a schema-level allowance, not a statement that it's normally absent.

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

**Reason:** The entire purpose of archiving is to let the live tables shrink; a live FK from an
archive row back to a live row would either dangle once the live row is deleted (breaking
referential integrity) or force `on_delete=CASCADE`, which would silently delete archive history
whenever a live wallet is deleted — the opposite of the "archive instead of delete" goal. This is
the standard shape for audit/log tables.

**Cost to revisit:** Low — archive tables are additive/audit-only by nature; changing their shape
never risks live data since nothing else needs to be able to join through them.

## 7. `LicenseModifier.factor` precision: `max_digits=10, decimal_places=6`

**Dilemma:** README examples (`0.5`, factor implicit in `("credits", 3.27)` conversion rates) only
need 2 decimal places and are always well under 10.

**Decision:** Smaller precision than the ledger fields (`10/6` vs `20/6`) since a factor is a
multiplier, not an accumulating balance — it never needs the wide integer part a balance might.

**Reason:** Right-sizing: a cost-adjustment factor realistically stays within a few orders of
magnitude of 1.0; giving it the same 20-digit integer headroom as a wallet balance would be
needless.

**Cost to revisit:** Low.

## 8. Python/Django app naming: package `django_licensing`, app label `django_licensing`

**Dilemma:** README never states the intended Python import path or Django `INSTALLED_APPS` label
— just the pip-installable name `django-licensing` used throughout prose.

**Decision:** Import package and app label both `django_licensing` (underscored form of the pip
name), single `apps.py` with `DjangoLicensingConfig`, models in one `models.py` (not a `models/`
package) since concern 1 has only six models and splitting them now would add indirection without
current benefit.

**Reason:** Follows the common convention for `django-*` packages (pip name has dashes, import
path has underscores) and keeps the KlubIS `setup`-module integration point predictable: `INSTALLED_APPS
+= ["django_licensing"]`.

**Cost to revisit:** Very low pre-1.0, but a rename after publishing to PyPI/being pinned by
KlubIS would be a breaking change for that consumer. Worth confirming before KlubIS actually adds
the dependency.

## 9. Test database: Postgres, not SQLite

**Dilemma:** README doesn't mandate a test database engine.

**Decision:** Postgres (`tests/env.py`, gitignored, mirrors the `klubis-novi` `setup/env.py`
convention; template in `tests/env.example.py`), using the same local `klubis` Postgres role
already used by the `klubis-novi` project, but two dedicated databases (`django_licensing`,
`test_django_licensing`) — never the `klubis-novi` database itself.

**Reason:** Production is Postgres (per `klubis-novi`'s own `CLAUDE.md`, and per this library's
own `JSONField`/`Decimal` usage), and SQLite's looser type affinity and different `JSONField`
backing can mask precision/constraint bugs (e.g. the `UniqueConstraint` with a `condition=` on
`Transaction.storno_of` relies on partial-index support, which SQLite via Django does support but
less battle-tested than Postgres). Testing against the real target engine from day one avoids a
class of "works in tests, breaks in prod" surprises.

**Cost to revisit:** Low — swapping to SQLite later (e.g. for a faster CI matrix) is a
`tests/settings.py` change only, no model changes expected.

---

Autonomous-session decisions made while implementing **Concern 2 (fulfillment engine)** —
`django_licensing/engine.py` and `django_licensing/providers.py`. Same rules as above: dilemma,
decision, reason, cost to revisit.

## 10. `Wallet.wallet_type` (grant/consumable) does not change *how* consumption works — only draw
priority

**Dilemma:** README's Play-Billing-inspired language ("grant" = boolean entitlement, "consumable" =
spendable budget) suggests grant wallets might not decrement at all when drawn from — they'd just
gate a boolean check. But the `fulfillment_order` examples draw from grant wallets *before*
consumable ones for the exact same feature and quantity, and `Wallet.balance`/`overdraft_limit`/
`first_overdraft_at` are shared fields with no per-type branching in the concern-1 schema.

**Decision:** Both wallet types are spent identically by `fulfill()` — same balance arithmetic,
same `Transaction.kind = CONSUMPTION` regardless of which section funded the draw. `wallet_type`
only changes *which* wallets a `fulfillment_order` step selects (i.e., priority/funding-source
tier — "spend free grants before paid consumable budget"), never the mechanics of spending itself.

**Reason:** This is the only reading consistent with the `fulfillment_order` examples actually
walking both types with the same quantity/currency semantics, and it keeps `TransactionKind.GRANT`
meaningful as "a credit/top-up event" (something a host application writes directly when funding a
wallet — see README "Out of Scope" → "Granting wallets... is the responsibility of the
application") as opposed to "a consumption event that happened to come from a grant-type wallet".

**Cost to revisit:** High — this is the central mechanical assumption `_consume()` is built on. If
a future session decides grant wallets should instead behave as non-decrementing boolean gates,
`_consume()`/`_build_ordered_wallets()` need a real branch, not just a priority-ordering role, for
`WalletType.GRANT`.

## 11. Modifier "most favourable" is taken literally as `max(factors)`, even though it reads
economically backwards

**Dilemma:** README: "A modifier of 0.5 on `send_email` means each send costs half a unit... Where
multiple modifiers apply, the most favourable (`max`) is used." A factor of 0.5 is *cheaper* than
1.0, so intuitively the smaller factor is "more favourable" to the subject — which would suggest
`min()`, not `max()`.

**Decision:** Implemented literally as written: `_max_modifier_factor()` returns `max(factors)`.

**Reason:** The README parenthesizes `(max)` immediately after "most favourable", which reads as
the author defining what "favourable" means in this system rather than leaving it to be inferred —
taking the text at its word is safer than substituting an economic intuition the author may not
share (e.g. "favourable" from the *platform's* revenue-protection point of view, not the subject's
cost point of view). See project memory: prefer verifying the stated rule over inventing a
"friendlier" one.

**Cost to revisit:** Low — `max(factors)` → `min(factors)` is a one-line change in
`_max_modifier_factor()`; no schema impact. Flagging clearly here since it's the one place in this
concern where literal-README and intuitive-README diverge, and a KlubIS consumer should sanity
this before relying on it for a real discount/surcharge modifier.

## 12. Overdraft: feature-level flag gates *whether* a second pass runs; per-wallet
`overdraft_limit` (not `None`) gates *which* wallets it can draw from

**Dilemma:** README/NEXT_STEPS say "a second pass is made over wallets with `overdraft: true`" —
but `overdraft` is a *feature*-level boolean in the settings example, and `Wallet` has no boolean
field at all, only a nullable `overdraft_limit` Decimal.

**Decision:** `feature_def.overdraft` (bool) controls whether `fulfill()`/`peek()` attempt a second
pass at all once the first pass leaves `remaining > 0`. Within that second pass, a wallet is
eligible only if its own `overdraft_limit is not None`; capacity in that pass is
`max(balance, 0) + overdraft_limit` (i.e. the wallet can go negative down to `-overdraft_limit`).

**Reason:** This is the only combination that uses every field concern 1 actually built
(`Wallet.overdraft_limit`, `Wallet.first_overdraft_at`) while also honouring the feature-level
`overdraft` switch from the settings example — a feature can opt out of overdraft entirely even if
some of its wallets happen to carry a limit (e.g. a limit configured for a *different* feature that
also draws from the same wallet).

**Cost to revisit:** Medium — changing which of the two flags is authoritative (or requiring both)
is a behavioural change with no schema migration needed, but would change existing hosts' overdraft
outcomes silently if not documented.

## 13. Sliding-window `reset_config`: `None`/falsy means "not a sliding-window wallet at all", and
concrete JSON key names for the four table rows

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
  non-empty, e.g. `{}` would be unusual but still resolves to a 24h rolling window rather than
  erroring).
- Daily: `{"time": "HH:MM:SS"}`
- Weekly: `{"day_of_week": 0-6, "time": "HH:MM:SS"}` (0 = Monday, matching `datetime.weekday()`)
- Monthly: `{"day_of_month": 1-31, "time": "HH:MM:SS"}` (clamped to the last day of shorter months)

For a sliding-window wallet, `wallet.balance` is redefined as the **per-window allotment**
(constant, never mutated by `fulfill()`/`unfulfill()`); `effective_balance()` computes
`balance + sum(transactions within the current window)` on every call — see `window_start()` and
`effective_balance()` in `engine.py`.

**Reason:** This is the only reading that (a) keeps every existing concern-1 test and the
`Wallet.balance` docstring ("checkpoint... not the effective balance" — only stated for
sliding-window wallets) true without a migration, and (b) gives the rolling-window case an actual
place to store its window length, which the README never specifies. The "checkpoint = allotment"
model for sliding-window wallets specifically (rather than a running total) is what makes "resets
every window" a real, computed behaviour instead of requiring a scheduled job to zero the balance
out — matching the README's "computed on-the-fly" framing.

**Cost to revisit:** Medium-high — this is a genuine judgment call with no README text to lean on
beyond the four-row table and the "computed on-the-fly" sentence. If a future session (or KlubIS
itself, as the first real consumer) wants `reset_config = None` to mean "rolling window with an
implicit default length" instead, `effective_balance()`'s guard condition
(`if not wallet.reset_config: return wallet.balance`) is the single place to change, but every
sliding-window test in `tests/test_engine_sliding_window.py` would need its wallets given an
explicit `reset_config` dict instead of relying on the current default-off behaviour.

## 14. Rounding reconciliation: `qty_covered` is back-derived from the rounded currency cost, and
clamped rather than exactly reconciled

**Dilemma:** README states rounding is per-currency (floor/ceil/round/None) but never says how to
reconcile a rounded wallet-currency debit against the feature-unit quantity being fulfilled — e.g.
if `ceil` rounds a fractional cost up to more currency than the wallet actually holds, or up to more
than corresponds to the remaining requested quantity.

**Decision:** After rounding, if the rounded cost would exceed the wallet's available capacity, it
is clamped down to exactly that capacity (never overdraws beyond what was already established as
available). The feature-unit quantity credited for that draw (`qty_covered`) is then derived by
dividing the (possibly clamped) rounded cost back by the effective rate, and is itself clamped to
never exceed the remaining requested quantity. In edge cases (e.g. `ceil` on a very small fractional
rate) this can charge a currency unit slightly out of exact proportion to the quantity nominally
granted for that specific wallet's draw.

**Reason:** No exact reconciliation is possible in general once rounding is introduced — some
"error" has to land somewhere (the wallet, the requester, or be silently absorbed). Clamping to
capacity guarantees the one invariant that matters most for a ledger library: a wallet's balance
never goes past its (overdraft-adjusted) floor because of a rounding artifact. Tests
(`test_fulfill_rounding_ceil`/`floor`/`round` in `tests/test_engine_fulfill.py`) were deliberately
built with round numbers that don't hit this edge case, so this is documented as a known
approximation rather than exhaustively tested.

**Cost to revisit:** Low-medium — confined entirely to `_consume()` in `engine.py`; no schema
impact. Worth a second look before this library is used for a currency where the requester cares
about exact-to-the-unit reconciliation (e.g. legal/financial per-cent accounting) rather than
"close enough, never overdraws".

## 15. Archival trigger split into two mechanisms: automatic "drained" trigger inside `fulfill()`,
plus a separate `archive_expired_wallets()` maintenance entry point

**Dilemma:** README says "Fully consumed wallets are archived rather than deleted" but never
defines "fully consumed", and a wallet can become permanently unusable in two different ways that
`fulfill()` can't uniformly observe: (a) its balance is drawn down to zero with no overdraft left,
which `fulfill()` *does* see (it just performed that draw); (b) its `valid_until` passes with the
wallet never touched again, which nothing observes unless something polls for it — a wallet that
expires without ever being fully drawn is invisible to `fulfill()`'s per-draw hook entirely, since
`_validity_q()` already excludes expired wallets from every candidate query.

**Decision:** Two separate mechanisms:
- `_archive_if_drained(wallet)` runs inside `fulfill()`'s commit loop, immediately after a wallet is
  drawn from, for wallets where `reset_config` is falsy (non-sliding-window) and
  `balance <= 0 and overdraft_limit is None` (no possible future draw without a plan change).
  Sliding-window wallets are never archived by this trigger — they are designed to replenish every
  window, so "drained right now" carries no permanence.
- `archive_expired_wallets(at=None)` is a standalone function a host application calls periodically
  (e.g. Celery beat) to sweep wallets whose `valid_until` has passed, regardless of whether they
  were ever drawn to zero.

**Reason:** Splitting these was the only way to cover both cases without `fulfill()` scanning
unrelated wallets it never touched on every call (expensive, and out of scope for a function whose
job is to fulfil one request). This mirrors the existing "recovery flow" pattern the README already
expects (`find_unattached_fulfillment_records()`, README: "A background job may monitor...") —
maintenance sweeps are explicitly the host application's job, this library just provides the query/
action.

**Cost to revisit:** Low — both are additive; a future session could add more trigger conditions
(e.g. archive on wallet close via `plan_switch`) without touching either existing path.

## 16. `Transaction.storno_of` is `on_delete=PROTECT`, so archiving must delete storno rows before
their originals

**Dilemma:** Not a README ambiguity but a concrete bug hit while implementing archival: Django's
`PROTECT` raises `ProtectedError` whenever *any* row references the one being deleted — even if
that referencing row is *also* being deleted in the very same cascade. `wallet.delete()`'s
CASCADE-based collection of the wallet's transactions therefore fails outright the moment a wallet
has both an original and its storno transaction, because the storno's `storno_of` FK protects the
original from deletion regardless of the storno also being slated for deletion.

**Decision:** `_archive_wallet()` explicitly deletes all storno transactions for the wallet (the
referencing side of `storno_of`) *before* calling `wallet.delete()`, so nothing is left pointing at
an original by the time the cascade runs.

**Reason:** Storno-of-storno never happens in practice (the one-storno-per-transaction DB
constraint from concern 1, and `unfulfill()`'s own `.exclude(kind=STORNO)` guard, both prevent it),
so a single flat "delete every storno row first" pass is sufficient — no need for a general
topological-delete routine.

**Cost to revisit:** Very low — purely an implementation detail inside `_archive_wallet()`; would
only need revisiting if a future concern introduces multi-level storno chains.

## 17. `plan_switch`'s `subjects` × `open_wallets` fan-out: every subject gets its own full copy of
every `WalletSpec`

**Dilemma:** README's Core API signature is `plan_switch(subjects, open_wallets, effective_date=None)`
with `open_wallets: list[WalletSpec]` — but never states whether `subjects` is a single subject
wrapped for API consistency, or genuinely plural, nor how a `WalletSpec` (which carries no subject
field) maps onto multiple subjects if so.

**Decision:** `subjects` is a `dict[str, Model]` exactly like `peek`/`fulfill`. Every value in that
dict has ALL of its currently-valid wallets closed (`valid_until = effective_date`), and then
receives its own newly created wallet for every `WalletSpec` in `open_wallets` — i.e. N subjects ×
M specs = N×M new wallets, not M shared wallets somehow co-owned by N subjects.

**Reason:** `Wallet.subject` is a single `GenericForeignKey`, so a wallet can only ever be owned by
one subject — there is no schema-level way for two subjects to "share" one new wallet. Giving each
subject its own full copy of the plan is the only interpretation that produces valid `Wallet` rows
at all.

**Cost to revisit:** Low — if a future caller only ever wants to switch a single subject's plan at a
time (the more common real-world case, e.g. one member's individual plan change), the multi-subject
fan-out is simply unused, not wrong; no migration implied either way.

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
example shows rates declared explicitly per feature. Doing nothing beyond what's demonstrated avoids
guessing a mechanism the README doesn't actually specify.

**Cost to revisit:** Low — purely additive; a future concern could add "if a feature accepts no
currency explicitly, fall back to all `general=True` currencies at rate 1" without touching any
existing behaviour, since today's behaviour (nothing implicit happens) is a strict subset.
