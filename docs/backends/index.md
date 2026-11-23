# How the port works

The engine selects candidate wallets, walks them in fulfilment order, converts at rates, rounds, runs the overdraft
pass and computes window balances without accessing a database. It reads and writes state through a **store** with
twenty-two methods; the rest of the engine is plain Python. Every store method is a coroutine, and the engine awaits
nothing but store methods.

```text
      your application
             │
             ▼
   allowances.engine          peek · fulfill · unfulfill · transfer · add_member · plan_switch
             │
     ┌───────┴────────┐
     ▼                ▼
 Store port    DefinitionProvider
     │                │
     ▼                ▼
 DjangoStore     SettingsProvider
 AdormStore      DictProvider
 MemoryStore     (yours)
 (yours)
```

The **store** holds state; the **provider** supplies definitions. They are independent of each other, e.g. a Django
store with definitions from a remote service.

## What a store owes the engine

The full interface is in [the API reference](/api/core#store). Every method except `unit_of_work` is a coroutine
function; `unit_of_work()` returns an asynchronous context manager. A store must meet four obligations:

**Ordering.** Wallets come back oldest-first, by `(created_at, id)`. The engine uses that order as the tie-breaker
within a fulfilment step; an unstable order makes the same request draw from different wallets on different runs.

**Atomicity.** `unit_of_work()` makes everything inside it commit together or not at all, and it must nest: the engine
opens one, and a caller may already have opened another around it. A nested block that raises discards only its own
writes.

**Locking.** `lock_wallets` and a locked read (`lock=True`) must stop a concurrent operation from changing the same
rows until the block ends, and `lock_memberships` must serialise membership changes until it ends. Without them, two
simultaneous calls can each read the same balance and each spend it, or two additions can together form a membership
cycle. A backend that cannot lock states so in its documentation.

**Lock order.** An engine operation locks all the wallets it uses in one `lock_wallets` call, before reading the
balances it relies on, and locks no further wallet afterwards; a fulfilment record is locked before its wallets.
`lock_wallets` takes its locks in ascending wallet id, so two operations never wait for each other in a cycle. Wallet
ids of one store must therefore be mutually comparable: integers, or strings, but not a mix.

## Stores that never suspend

A store whose methods and `unit_of_work` await nothing that yields to an event loop also serves the synchronous API,
[`allowances.sync`](/guide/synchronous), which runs the engine's coroutines to completion without an event loop.
`MemoryStore` and `allowances_django.sync.store.DjangoStore` are such stores. A store that suspends (the asynchronous
`DjangoStore`, `AdormStore`) serves only the asynchronous API; `allowances.sync` raises `StoreSuspendedError` for it.

## Why the port exists

**Testing.** Engine tests need no database: the library's engine tests run against the in-memory store, both Django
stores and the adorm store.

**Database independence.** Engine code that requires ORM features fails on the in-memory backend.

**Other backends.** A Redis store for hot-path quotas, a store over a remote entitlement service or a read-only store
over an existing billing schema need no engine changes. See [Redis as a backend](/backends/redis) for where Redis fits.

## Backends that ship

| | |
| --- | --- |
| [Django](/backends/django) | Relational, transactional, row locking. Two stores: one for the asynchronous API (queries on threads), one for the synchronous API. For production. |
| [adorm](/backends/adorm) | The Django tables through adorm: asynchronous I/O on PostgreSQL (asyncpg) or the Django ORM on adorm's threads. Asynchronous API only. |
| [In-memory](/backends/memory) | Dict-based, never suspends. For tests; not for production. |

For anything else, see [Writing a backend](/backends/writing-a-backend).

## Partial backends

An append-only or cache-shaped backend may have no archive to move wallets into, and no way to answer a scan like
`records_with_unattached_transactions`.

Such a backend raises `UnsupportedOperationError` instead of doing nothing: a no-op archive leaves expired wallets in
the live tables without any error.
