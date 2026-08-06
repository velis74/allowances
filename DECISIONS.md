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
