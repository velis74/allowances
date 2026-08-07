# Reversals and recovery

Quota is consumed *before* the work happens — you take the email allowance, then send the email. Which means the interesting failure is the one in between: the allowance is gone and the email never went out.

Three pieces handle this: `attach` records success, `unfulfill` undoes a failure, and a query finds the ones that did neither.

## Attach: recording what it paid for

```python
result = fulfill("send_email", 1, {"user": user})
message = send_the_email()
attach(result.guid, message)
```

`attach` points every not-yet-attached transaction under that guid at the object. It leaves already-attached ones alone, so calling it again with a different object is a no-op rather than a rewrite.

Doing this consistently is what makes the rest work. An attached transaction is quota that bought something real. An unattached one is quota in limbo.

## Unfulfill: giving it back

```python
unfulfill(result.guid)
```

Nothing is deleted and nothing is edited. Each original entry gets a matching `storno` entry of the opposite sign, pointing back at what it reverses. The ledger keeps showing that 3 credits were taken and 3 credits were given back — which is what you want when a customer asks, and what a deletion would have destroyed.

Some properties worth relying on:

**It is idempotent.** A transaction that already has its storno is skipped. Calling it twice reverses nothing twice.

**The database enforces it too.** A unique constraint on `storno_of` means a lost race produces an integrity error rather than a silent double refund.

**It is a unit.** Every transaction under the guid is reversed together, across however many `fulfill` calls shared it and however many wallets they touched.

**An unknown guid raises.** `RecordNotFoundError`, not a silent no-op — a guid nobody ever fulfilled under means the caller has lost track of which operation it was undoing.

**Archived wallets are skipped.** If a wallet was drained to zero and [archived](/guide/plans-and-archival), it is gone from live storage and there is nothing left to credit. The call still succeeds.

::: warning Reversing across a window boundary
For a [sliding-window](/guide/sliding-windows) wallet, reverse inside the same window and the storno nets the consumption straight back out. Reverse after the window has passed and the original has already aged out — so the storno credits a window that never paid for anything. Keep recovery flows short relative to the window, or accept the drift knowingly.
:::

## Finding what broke

A transaction with no attached object is a fulfillment whose business operation may have died halfway:

```python
from datetime import timedelta

from django.utils import timezone

from allowances_django.shortcuts import find_unattached_fulfillment_records, unfulfill

# Anything unattached for more than an hour is almost certainly not coming back.
cutoff = timezone.now() - timedelta(hours=1)

for record in find_unattached_fulfillment_records(older_than=cutoff):
    unfulfill(record.guid)
```

The age cutoff is the whole design. Without it you would sweep up operations that are merely still in flight; with it, "old and unattached" is a good proxy for "the process died".

Run it from a periodic job. How aggressive to be is a policy call — refunding too eagerly hands back quota for work that did happen, refunding too slowly leaves customers short.

::: tip This is why `attach` is not optional
Skip it and every fulfillment looks like a failure to this query. If some operations genuinely have nothing to attach, attach something anyway — the record they belong to, a log row, anything stable — or exclude those features from the sweep.
:::

## Downgrades

When a subject's entitlement shrinks — a plan downgrade from 10 seats to 3 — the library will not go and find what now exceeds the limit. It can't: only your application knows which seven users to remove.

The supported flow is to use `peek` to ask what still fits, decide which domain objects to drop, and `unfulfill` the fulfillment records belonging to them. That is why `attach` matters here too: it is the link from an over-limit object back to the quota it holds.
