# Reversals and recovery

`fulfill` runs before the business operation: the quota is consumed, then the email is sent. When the operation fails,
the consumed quota has to be returned.

Three functions handle this: `attach` records success, `unfulfill` reverses a failure, and
`find_unattached_fulfillment_records` finds fulfilments that have neither.

## Attach: recording what it paid for

```python
result = fulfill("send_email", 1, {"user": user})
message = send_the_email()
attach(result.guid, message)
```

`attach` points every not-yet-attached transaction under that guid at the object. It leaves already-attached ones
unchanged, so calling it again with a different object changes nothing.

The recovery sweep treats a consumption entry without an attached object as a possibly failed operation.

## Unfulfill

```python
unfulfill(result.guid)
```

Nothing is deleted and nothing is edited. Each original entry gets a matching `storno` entry of the opposite sign,
pointing back at the entry it reverses. The ledger shows both that 3 credits were taken and that 3 credits were given
back.

Properties:

**It is idempotent.** A transaction that already has its storno is skipped. A second call writes nothing.

**The database enforces it.** `storno_of` has a unique constraint: a second storno of the same entry, for example from
two concurrent calls, fails with an integrity error.

**All entries under the guid are reversed together**, across every `fulfill` call that shared the guid and every
wallet they drew from.

**An unknown guid raises** `RecordNotFoundError`.

**Credits reverse the same way.** A [`credit`](/guide/credits-and-budgets) filed under a guid gets a negative storno,
which takes the amount back out of the wallet, below zero if part of it has been spent.

**Archived wallets are skipped**, except the wallets of a [transfer](#reversing-a-transfer). If a wallet was drained to
zero and [archived](/guide/plans-and-archival), it is no longer in live storage; the call succeeds and writes no storno
for it.

**The storno records who reversed it.** `unfulfill(guid, actor=support_agent)` puts that actor on every storno entry;
the original entries keep theirs.

## Reversing a transfer

A [`transfer`](/guide/credits-and-budgets#moving-money-between-wallets) writes its debit and credit under one record,
and `unfulfill(result.guid)` reverses both: the amount goes back to the source and comes out of the target.

The target has to still hold the amount. The check nets every storno of the record on the target, so consumption paid
from the transferred amount under the same guid is given back first. When the target's balance
(`max(effective_balance, 0)`) plus that net change is below zero (part of the amount has been spent under another
guid), or the target has been archived, `unfulfill` raises `InsufficientBalanceError` and writes nothing, for that
transfer and for everything else under the same guid. To return the unspent part, make a new `transfer` in the
opposite direction.

The source has to still exist. When it has been archived since (drained to zero by later consumption), `unfulfill`
raises `WalletNotFoundError` and writes nothing.

::: warning Reversing across a window boundary
For a [sliding-window](/guide/sliding-windows) wallet, a reversal inside the same window cancels the consumption. After
the window has passed, the original consumption no longer counts, so the storno is a positive entry in the current
window and raises its balance above the allotment. Reverse within the window to avoid it.
:::

## Recovery sweep

A consumption entry with no attached object is a fulfilment whose business operation may have failed. Credits and
transfers are complete when written and are not listed:

```python
from datetime import timedelta

from django.utils import timezone

from allowances_django.shortcuts import find_unattached_fulfillment_records, unfulfill

# Records unattached for over an hour count as failed.
cutoff = timezone.now() - timedelta(hours=1)

for record in find_unattached_fulfillment_records(older_than=cutoff):
    unfulfill(record.guid)
```

`older_than` excludes operations still in progress; set it above the longest duration of the operation.

Run it from a periodic job. A cutoff shorter than an operation reverses the consumption of operations that succeed; a
longer one delays returning the quota of failed ones.

::: tip `attach` and the recovery sweep
Without `attach`, every fulfilment is returned by this query. For operations with no natural object, attach a stable
object instead (the record they belong to, a log row), or exclude those features from the sweep.
:::

## Downgrades

When a subject's entitlement shrinks (a plan downgrade from 10 seats to 3), the library does not select the objects
over the new limit; the application chooses them.

Use `peek` to check what still fits, decide which domain objects to drop, and `unfulfill` the fulfilment records
belonging to them. `attach` links each object to the fulfilment record of its quota.
