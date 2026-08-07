# Would Redis work?

Partly. It is an excellent fit for one half of this library and a poor one for the other, and the useful answer is which half is which.

No Redis backend ships. This page exists so that the decision to write one is made with the tradeoffs visible.

## Where Redis is genuinely better

**Sliding windows are a native Redis shape.** A [windowed wallet](/guide/sliding-windows) is the textbook sorted-set rate limiter: entries scored by timestamp, `ZREMRANGEBYSCORE` to drop what aged out, `ZRANGEBYSCORE` to sum what's left. In Postgres the same thing is an aggregate over an index. Redis was built for it.

**Atomicity without locking.** The whole planning-and-writing sequence of a `fulfill` can be one Lua script, which Redis runs to completion with nothing else interleaved. No `SELECT FOR UPDATE`, no lock contention, no deadlock risk. For a hot path this is genuinely nicer than what the relational backend can do.

**Throughput.** Quota checks on every request, at rates where a database round-trip per check is the bottleneck, is exactly the workload Redis exists for.

## Where it fails

**The ledger is append-only and unbounded.** This library never edits a transaction and never deletes one while it matters. That history is the point — it is what answers "why are my credits gone". In Redis it is RAM, growing forever. You would need a compaction or eviction strategy, and every one of those trades away some of the auditability you were storing it for.

**Durability is approximate.** With AOF at `everysec` you can lose up to a second of writes on an unclean shutdown. For a cache that is irrelevant. For a ledger where the entries *are* the record of what a customer was charged, a second of silently vanished entries is a real if small liability — and worse, an invisible one.

**Ad-hoc queries need hand-built indexes.** `records_with_unattached_transactions` is one `WHERE` clause in SQL. In Redis it needs a secondary index you maintain yourself, kept consistent across every write path, including the failure ones. Same for admin browsing, reporting, and every "which wallets are in overdraft" question you haven't thought of yet. Each is a set to maintain, and each is a chance to drift.

**The archive doubles the problem.** Archived wallets and their entire transaction history would also be in RAM — the data you keep precisely because you rarely read it, held in your most expensive storage.

## What to build instead

**Hybrid, not replacement.** Redis for balances and window arithmetic; a relational store for the ledger. The port makes this expressible — a store that writes entries through to Postgres while keeping windowed balances in Redis. More work than either alone, and it gets both properties.

**Or Redis for a genuinely disposable subset.** If some features are pure rate limiting — "no more than 200 an hour", where nobody will ever ask for an audit trail and losing a second of history costs nothing — those can live in a Redis store with `UnsupportedOperationError` on the archive and scan methods. Keep billable entitlements in Postgres. Two stores, chosen per feature.

## If you write one anyway

- Do the whole `fulfill` sequence in Lua. Read-then-write from the client reintroduces exactly the race the script would have avoided.
- Persist with AOF at `always` if the ledger matters, and measure what it costs before assuming it's fine.
- Raise `UnsupportedOperationError` from what you haven't indexed, rather than returning an empty list. An empty list is indistinguishable from "nothing to report".
- Run [this library's engine test suite](/backends/writing-a-backend#testing-yours) against it. It is written against the port, so it will run against your backend unmodified.
