---
layout: home

hero:
  name: allowances
  text: Grants and budgets, enforced and auditable
  tagline: Ask "may this happen, and can they afford it?" — get an answer, a ledger entry, and a way to undo it. Storage-agnostic, with a Django backend.
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
  - title: Two kinds of right, one flow
    details: A boolean grant ("has a Pro subscription") and a consumable budget ("200 emails a day") coexist in the same fulfillment call, drawn in an order you declare.
  - title: Every unit accounted for
    details: Nothing is edited in place. Consumption writes a ledger entry, reversal writes a matching storno, and spent-out wallets move to an archive rather than disappearing.
  - title: Spend from several sources
    details: One operation can be paid for out of a specific budget, then general credits, then a promotional grant — at conversion rates you set, with a deterministic priority.
  - title: No ORM in the core
    details: The engine talks to a sixteen-method storage port. Postgres via Django ships with it; anything else is an afternoon's work.
---
