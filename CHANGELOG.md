# Changelog

## 0.3.0 — 2026-11-05

Upgrade steps: [Upgrading to 0.3](https://docs.velis.si/allowances/guide/upgrading).

### Added

- `credit()`: adds to a wallet's balance as a `grant` ledger entry, attached to the paying object;
  with a `guid` it is reversible by `unfulfill`.
- `open_wallet()`: opens one wallet with a `source`. Wallets whose source is not `"plan"` (purchased
  credits, budgets) are never closed or adjusted by `plan_switch`.
- `WalletSpec.on_switch` with `SwitchPolicy.REPLACE` (default), `ADJUST` and `TOP_UP`: a plan switch
  can keep a plan wallet and move its balance by the difference between the old and the new
  allotment.
- `adjust_wallet()`: changes the allotment of a wallet opened with `ADJUST` or `TOP_UP`.
- `consumed()`: net consumption of a wallet in a period, for billing postpaid budgets.
- `fulfillment_order` steps take an optional third element, `source`.
- Scheduled plan switches (future `effective_date`) support every switch policy, are cancelled by
  `cancel_scheduled_switch()` until they take effect, and are replaced by the next switch for the same
  subject.
- `Wallet.source`, `Wallet.allotment` and `Wallet.scheduled`; Django migration
  `0002_wallet_source_allotment_scheduled`.
- `WalletNotFoundError`, `UnsuitableWalletError`.
- Guide page "Credits and budgets" with recipes for prepaid credits, monthly plan credits and
  postpaid budgets.

### Changed

- `plan_switch` closes only wallets with `source == "plan"`. Existing wallets default to `"plan"`.
- Within a `fulfillment_order` step, wallets are drawn soonest `valid_until` first, wallets without
  one last; equal expiries keep oldest-first order.
- Wallets with an `allotment` are not archived when drained.
- `credit()` refuses a wallet that is not yet valid.
- Store port (breaking for custom backends): `create_wallet` takes `source`, `allotment` and
  `scheduled`; `save_wallet` also persists `overdraft_limit`, `reset_config`, `allotment` and
  `scheduled`; `find_wallets` accepts `currencies=None`; `close_wallets` is removed; new methods
  `find_scheduled_wallets` and `transactions_for_wallet`.

### Fixed

- `allowances.__version__` matches the package version.
