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
- `Wallet.source` and `Wallet.allotment`; Django migration `0002_wallet_source_and_allotment`.
- `WalletNotFoundError`, `UnsuitableWalletError`.
- Guide page "Credits and budgets" with recipes for prepaid credits, monthly plan credits and
  postpaid budgets.

### Changed

- `plan_switch` closes only wallets with `source == "plan"`. Existing wallets default to `"plan"`.
- Within a `fulfillment_order` step, wallets are drawn soonest `valid_until` first, wallets without
  one last; equal expiries keep oldest-first order.
- Wallets with an `allotment` are not archived when drained.
- Store port (breaking for custom backends): `create_wallet` takes `source` and `allotment`,
  `save_wallet` also persists `overdraft_limit`, `reset_config` and `allotment`, `close_wallets`
  takes `source` and `exclude`, new method `transactions_for_wallet`.

### Fixed

- `allowances.__version__` matches the package version.
