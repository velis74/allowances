---
title: Python quotas, credits and entitlements
description: 'Python grants and consumable budgets paid from several wallets, with an append-only ledger and storno, and a Django backend.'
layout: home

hero:
  name: allowances
  text: Grants and budgets with a ledger
  tagline: Checks a grant or budget, records each consumption in a ledger and reverses it with a storno. Storage-agnostic, with a Django backend.
  actions:
    - theme: brand
      text: What it is
      link: /guide/
    - theme: alt
      text: Getting started
      link: /guide/getting-started
    - theme: alt
      text: API
      link: /api/core

features:
  - title: Grants and consumable budgets in one call
    details: A boolean grant ("has a Pro subscription") and a consumable budget ("200 emails a day") are resolved in the same fulfilment call, in the declared fulfilment order.
  - title: Append-only ledger
    details: Ledger entries are not edited. Consumption writes a ledger entry, reversal writes a matching storno, and drained wallets are moved to an archive.
  - title: Several sources per operation
    details: One operation can draw from a specific budget, then general credits, then a promotional grant, at configured conversion rates and in a deterministic order.
  - title: Expiring, nested and recorded Django permissions
    details: AllowancesBackend answers has_perm from grant wallets of users, groups nested to any depth, organisations or devices. Rights expire at valid_until, come with plans or purchases, and each use can be recorded with its actor.
    link: /guide/permissions-quick-start
  - title: No ORM in the core
    details: The engine accesses storage through a 22-method store port. Django stores (Postgres, SQLite), an adorm store (adorm's Django driver or asyncpg) and an in-memory store are included; other backends implement the port.
  - title: Async-first, with a synchronous twin
    details: Every engine function and store method is a coroutine. allowances.sync has the same functions for Celery tasks and synchronous views, running them without an event loop on a store that never suspends.
    link: /guide/synchronous
---
