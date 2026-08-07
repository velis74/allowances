# How the port works

The engine does arithmetic. Deciding which wallets are candidates, walking them in order, converting at rates, rounding, running the overdraft pass, computing window balances — none of that needs to know what a database is.

So it doesn't. The engine receives a **store** and talks to it through sixteen methods. Everything else is pure Python.

```
      your application
             │
             ▼
   allowances.engine          peek · fulfill · attach · unfulfill · plan_switch
             │
     ┌───────┴────────┐
     ▼                ▼
 Store port    DefinitionProvider
     │                │
     ▼                ▼
 DjangoStore     SettingsProvider
 MemoryStore     DictProvider
 (yours)         (yours)
```

Two seams, not one. The **store** is where state lives; the **provider** is where definitions come from. They vary independently — a Django store with definitions from a remote service is a perfectly sensible combination.

## What a store owes the engine

The full interface is in [the API reference](/api/core#store). Three obligations are worth stating separately, because they are the ones that are easy to get subtly wrong:

**Ordering.** Wallets come back oldest-first, by `(created_at, id)`. The engine treats that order as the tie-breaker within a fulfillment step, so an unstable order means non-deterministic draws — the same request charging different wallets on different runs.

**Atomicity.** `unit_of_work()` makes everything inside it commit together or not at all, and it must nest: the engine opens one, and a caller may already have opened another around it.

**Locking.** `lock=True` must stop a concurrent fulfillment from seeing the same rows until the block ends. Without it, two simultaneous calls can each read the same balance and each spend it. A backend that can't lock should say so in its documentation rather than pretend.

## Why bother

**Testing.** The engine's behaviour is arithmetic, and proving arithmetic doesn't need Postgres. This library's own engine tests run against a dict in well under a second, and again against Django to prove the two agree.

**It keeps the design honest.** Every time the engine reaches for something only an ORM could do, the in-memory backend stops working. That is a useful alarm.

**It leaves the door open.** A Redis backend for hot-path quota, a store fronting a remote entitlement service, a read-only store over an existing billing schema — none of those require touching the engine. Whether Redis is actually a good idea is [a separate question](/backends/redis), and the honest answer is "partly".

## Backends that ship

| | |
| --- | --- |
| [Django](/backends/django) | Relational, transactional, locks properly. The production one. |
| [In-memory](/backends/memory) | A dict. For tests and for proving the port holds. |

And [writing your own](/backends/writing-a-backend) if neither fits.

## Partial backends

Not every store can do everything. An append-only or cache-shaped backend may have no archive to move wallets into, and no way to answer a scan like `records_with_unattached_transactions`.

Raise `UnsupportedOperationError` rather than silently doing nothing. A no-op archive looks exactly like a working one until the table is enormous.
