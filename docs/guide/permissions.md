---
description: 'Rights as rate-0 grant wallets: permanent and temporary rights, groups through membership, the actor in the ledger, and a grant-only Django permissions backend.'
---

# Rights and permissions

A right is something a subject may do, not something it spends. allowances holds rights as grant wallets, resolves them across users and the groups they belong to, records every use in the ledger, and answers Django's `has_perm` from them.

## A right is a rate-0 grant

A feature whose section accepts a currency at **rate 0** costs nothing out of a wallet of that currency. Such a wallet with a positive balance covers the whole request at cost 0; with a balance of 0 or less the right is not held.

```python
ALLOWANCES = {
    "currencies": {"club_admin": {}},
    "features": {
        "edit_members": {
            "grant": {"currencies": [("club_admin", 0)]},
            "fulfillment_order": [("grant", "user")],
        },
    },
}
```

```python
from decimal import Decimal

from allowances import WalletSpec, WalletType
from allowances_django.shortcuts import open_wallet

open_wallet(user, WalletSpec(WalletType.GRANT, "club_admin", Decimal("1")), source="role")
```

A grant wallet with rate 0, balance 1 and no `valid_until` is a **permanent right**. Rate-0 wallets never take part in the [overdraft](/guide/overdraft) pass, so `overdraft_limit` does not make a right out of an empty wallet.

## Checking and using a right

```python
from allowances_django.shortcuts import attach, fulfill, peek

peek("edit_members", 1, {"user": user}).satisfied     # a check: nothing is written

result = fulfill("edit_members", 1, {"user": user})    # a use: on the ledger
member.save()
attach(result.guid, member)
```

`peek` checks the right and writes nothing. `fulfill` records a use: a `consumption` entry of amount 0 under a fulfillment record, with its [actor](#the-actor). The balance does not change. The usual contract applies — `attach` the object the use was for, or `unfulfill` it, which writes `storno` entries of amount 0. `consumed()` counts nothing for a right.

Without the right, `peek` returns `satisfied=False` and `fulfill` raises `InsufficientBalanceError`.

## Temporary rights and revoking

A wallet with `valid_until` is a temporary right: it stops counting at that instant without anything running.

```python
WalletSpec(WalletType.GRANT, "club_admin", Decimal("1"), valid_until=end_of_season)
```

`close_wallet` revokes a right now (or at `at=`). It sets the wallet's `valid_until`, unless it already ends earlier, writes no ledger entry and leaves the balance as it is; `archive_expired_wallets` archives the wallet later.

```python
from allowances_django.shortcuts import close_wallet

close_wallet(wallet)
```

## Groups

A subject may be a member of other subjects. A group is any subject — an `auth.Group`, a team, a club, an organisation — and groups may be members of groups, to any depth.

```python
from allowances_django.shortcuts import add_member, groups_of, remove_member

add_member(user, board)
add_member(board, club)
groups_of(user)            # [board, club], nearest first
remove_member(user, board) # True
```

`add_member` raises `MembershipCycleError` when the group is the member itself or already one of its members, directly or through other groups; an existing membership is left as it is. Membership changes are serialised, so two concurrent `add_member` calls cannot form a cycle together.

`peek` and `fulfill` expand every subject passed to them with its groups under the same key: `{"user": user}` stands for the user, then the board, then the club. A step `("grant", "user")` therefore finds a right held by the user or by any group the user belongs to, the user's own wallets first. [Fulfillment order](/guide/fulfillment-order#memberships) describes the order in full. Removing a membership removes the rights that came through it.

## The actor

Every ledger entry records its **actor**, the subject on whose behalf it was made. `fulfill` called with one subject records that subject; with several, the `actor` argument, or none:

```python
fulfill("edit_members", 1, {"user": user})                       # actor: user
fulfill("send_email", 1, {"user": user, "team": team}, actor=user)
```

The actor is the subject the caller passed, never the group it expanded to: when the club's wallet pays for a member's use, the entry is on the club's wallet with the member as actor. `credit` and `transfer` take `actor=` and store it as given. `unfulfill(guid, actor=...)` records who reversed the entries on the storno entries; the originals keep theirs.

## The Django permissions backend

`allowances_django.permissions` holds Django permissions as rights.

A permission `"app_label.codename"` is the feature and the currency `"perm:app_label.codename"`; `permission_feature("club.change_member")` returns that name. These need no configuration: the shortcuts' default provider, `PermissionProvider(SettingsProvider())`, serves every `perm:` feature and currency that `settings.ALLOWANCES` does not define — the currency at rate 0, the feature with the fulfillment order `[("grant", None)]`, so a wallet of any subject passed, or of its groups, holds the permission.

```python
from allowances_django.permissions import grant_permission, permission_feature, revoke_permission

grant_permission(board, "club.change_member")
grant_permission(user, "club.view_report", valid_until=end_of_audit)
revoke_permission(board, "club.change_member")   # returns how many wallets it closed
```

`grant_permission` opens a grant wallet in currency `perm:<perm>` with balance 1 and source `"permission"`, which no `plan_switch` touches, from `valid_from` (default now) until `valid_until` (default: no end). `revoke_permission` closes every live grant wallet of that subject in that currency that has not ended yet.

A use of a permission goes on the ledger like any other right:

```python
result = fulfill(permission_feature("club.change_member"), 1, {"user": request.user})
```

### Configuring the backend

```python
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allowances_django.permissions.AllowancesBackend",
]
```

`AllowancesBackend` authenticates nobody and answers permission checks. Alone, it is the only source of permissions; chained after `ModelBackend`, Django grants a permission that either backend grants. `user.has_perm`, `{% if perms.club.change_member %}`, `@permission_required`, `PermissionRequiredMixin`, `ModelAdmin.has_*_permission` and DRF's permission classes all read it through `has_perm`.

`get_all_permissions(user)` is the permissions held by grant wallets of the user and of every group in `groups_of(user)` that are valid now and have a positive balance. An active superuser has every `Permission`; an inactive or anonymous user has none. `has_perm` and `has_module_perms` answer from that set. The set is read once per user object and cached on it (`_allowances_perm_cache`), as `ModelBackend` caches its own: a grant or revocation is seen by the next request's user object.

To give the members of an `auth.Group` a permission, make the user a member with `add_member(user, group)` and grant the permission to the group. Django's own `user.groups` is a separate table and does not count.

## Limits

- **No object-level permissions.** `has_perm(user, perm, obj)` with an `obj` is always false from this backend.
- **No deny rules.** A permission is held or not; nothing subtracts one granted through a group.
- **No admin UI for granting.** The admin lists wallets and memberships; `grant_permission`, `revoke_permission`, `add_member` and `remove_member` are the API.
