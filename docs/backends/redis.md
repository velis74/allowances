# Redis as a backend

Redis suits balances and sliding-window arithmetic; it does not suit the ledger and the archive.

No Redis backend ships. This page lists the trade-offs of writing one.

## Advantages

**Sliding windows.** A [windowed wallet](/guide/sliding-windows) maps to a sorted set scored by timestamp:
`ZREMRANGEBYSCORE` drops expired entries and `ZRANGEBYSCORE` returns the remaining ones, which a Lua script sums. In
PostgreSQL the same computation is an aggregate over an index.

**Atomicity without locking.** The whole planning-and-writing sequence of a `fulfill` can be one Lua script, which
Redis runs to completion with no other command interleaved. It needs no `SELECT FOR UPDATE` and cannot deadlock.

**Throughput.** Redis suits quota checks on every request where a database round-trip per check is the bottleneck.

## Disadvantages

**The ledger is append-only and unbounded.** The library never edits a transaction and never deletes a live one; the
ledger is the audit trail of every balance change. In Redis it occupies memory without bound. Compaction or eviction
keeps memory bounded but removes audit data.

**Durability is approximate.** With AOF at `everysec`, an unclean shutdown loses up to one second of writes. Lost
ledger entries are lost records of what a customer was charged, and nothing reports the loss.

**Ad-hoc queries need hand-built indexes.** `records_with_unattached_transactions` is one `WHERE` clause in SQL. In
Redis it needs a secondary index maintained by the store and kept consistent on every write path, failure paths
included. Admin browsing and reporting (for example, wallets in overdraft) each need another such index.

**Archive.** Archived wallets and their transactions would also be held in memory, although they are rarely read.

## Alternatives

**Hybrid store.** Balances and window arithmetic in Redis, the ledger in a relational store: one store that writes
entries through to PostgreSQL and keeps windowed balances in Redis. The port allows this; it is more work than either
store alone.

**Redis for rate-limit features only.** Features that need no audit trail and tolerate losing a second of history
(for example, "at most 200 an hour") can use a Redis store that raises `UnsupportedOperationError` from the archive and
scan methods. Billable entitlements stay in PostgreSQL: two stores, chosen per feature.

## Requirements for a Redis store

- Run the whole `fulfill` sequence in one Lua script. Reading and then writing from the client allows two calls to
  spend the same balance.
- Persist with AOF `appendfsync always` if the ledger is kept; it lowers write throughput.
- Raise `UnsupportedOperationError` from methods without an index instead of returning an empty list. An empty list
  cannot be told apart from "nothing to report".
- A store on `redis.asyncio` suspends and serves only the asynchronous API; one on the synchronous `redis` client that
  awaits nothing serves [`allowances.sync`](/guide/synchronous) as well.
- Run [the library's engine test suite](/backends/writing-a-backend#testing-a-backend) against it.
