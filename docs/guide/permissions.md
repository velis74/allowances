---
description: 'Rights as rate-0 grant wallets: how they compare with django.contrib.auth, permanent and temporary rights, nested groups, the actor in the ledger, effective permissions, and a Django permissions backend.'
---

# Rights and permissions

A right is something a subject may do, not something it spends. allowances holds rights as grant wallets, resolves them
across users and the groups they belong to, records every use in the ledger, and answers Django's `has_perm` from them.

To set it up in a Django project, follow the [Permissions quick start](/guide/permissions-quick-start).

## allowances or django.contrib.auth

Django's `ModelBackend` stores permissions as rows without a validity period: in `User.user_permissions` and in the
`Group.permissions` many-to-many, inherited only through `user.groups`. allowances holds a permission as a grant wallet
of any subject, with a validity period, inherited through memberships of any depth, and with a ledger for its uses.

| | `django.contrib.auth` (`ModelBackend`) | allowances (`AllowancesBackend`) |
| --- | --- | --- |
| Who holds a permission | users, and groups of users | any subject `(type, id)`: users, groups, organisations, devices, club memberships; `has_perm` is asked for users, other subjects are checked with `peek` / `fulfill` |
| Groups | one level: a user is in groups, groups contain no groups | any model as a group, nested to any depth, cycles rejected |
| Validity | permanent until removed | `valid_from` / `valid_until`; an expired right is not held, with no scheduled job |
| Where a right comes from | assigned by hand or in code | assigned (`grant_permission`), part of a plan (`plan_switch`) or bought (`open_wallet` with its own `source`) |
| Limited uses | no | a right usable *n* times: a wallet of balance *n* at a rate above 0, checked with `peek` / `fulfill` |
| Record of use | none | `fulfill` writes a ledger entry with the actor and the attached object |
| Revoking a group's right | applies to all members at once | applies to all members at once |
| Listing what a user holds and why | `get_user_permissions()` / `get_group_permissions()`: direct or through some group, without naming which group or until when | `effective_allowances` lists every right and balance with its holder and depth |
| Rights and quotas | separate mechanisms | one API: the same `peek` / `fulfill` call |
| Object-level permissions | none (needs a third-party backend) | none |
| Deny rules | none | none |
| Admin UI for granting | user and group forms | none; memberships cannot be added or changed in the admin, and wallets are editable only as raw rows, which bypasses the ledger |
| Storage and cost | `auth_*` tables; queries per user object, cached on it | `allowances_*` tables; a membership query per group level and one wallet query per user object, cached on it |

Django's permissions are enough for static staff roles edited in the admin. allowances is for rights that expire, are
bought or come with a plan, belong to subjects other than users, are inherited through nested groups, or whose every use
must be recorded. The two backends chain: listed together, a user holds a permission that either one grants.

## A right is a rate-0 grant

A feature whose section accepts a currency at **rate 0** costs nothing out of a wallet of that currency. Such a wallet
with a positive balance covers the whole request at cost 0; with a balance of 0 or less the right is not held.

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

await open_wallet(user, WalletSpec(WalletType.GRANT, "club_admin", Decimal("1")), source="role")
```

A grant wallet with rate 0, balance 1 and no `valid_until` is a **permanent right**. Rate-0 wallets never take part in
the [overdraft](/guide/overdraft) pass, so `overdraft_limit` does not make a right out of an empty wallet.

## Checking and using a right

```python
from allowances_django.shortcuts import attach, fulfill, peek

(await peek("edit_members", 1, {"user": user})).satisfied   # a check: nothing is written

result = await fulfill("edit_members", 1, {"user": user})  # a use: on the ledger
await member.asave()
await attach(result.guid, member)
```

`peek` checks the right and writes nothing. `fulfill` records a use: a `consumption` entry of amount 0 under a
fulfilment record, with its [actor](#the-actor). The balance does not change. The usual contract applies — `attach` the
object the use was for, or `unfulfill` it, which writes `storno` entries of amount 0. `consumed()` counts nothing for a
right.

Without the right, `peek` returns `satisfied=False` and `fulfill` raises `InsufficientBalanceError`.

## Temporary rights and revoking

A wallet with `valid_until` is a temporary right: it is not held from that instant on; no scheduled job is needed.

```python
WalletSpec(WalletType.GRANT, "club_admin", Decimal("1"), valid_until=end_of_season)
```

`close_wallet` revokes a right now (or at `at=`). It sets the wallet's `valid_until`, unless it already ends earlier,
writes no ledger entry and leaves the balance as it is; `archive_expired_wallets` archives the wallet later.

```python
from allowances_django.shortcuts import close_wallet

await close_wallet(wallet)
```

## Groups

A subject may be a member of other subjects. A group is any subject — an `auth.Group`, a team, a club, an organisation —
and groups may be members of groups, to any depth.

```python
from allowances_django.shortcuts import add_member, groups_of, remove_member

await add_member(user, board)
await add_member(board, club)
await groups_of(user)            # [board, club], nearest first
await remove_member(user, board) # True
```

`add_member` raises `MembershipCycleError` when the group is the member itself or already one of its members, directly
or through other groups; an existing membership is left as it is. Membership changes are serialised, so two concurrent
`add_member` calls cannot form a cycle together.

`peek` and `fulfill` expand every subject passed to them with its groups under the same key: `{"user": user}` stands for
the user, then the board, then the club. A step `("grant", "user")` therefore finds a right held by the user or by any
group the user belongs to, the user's own wallets first. [Fulfilment order](/guide/fulfillment-order#memberships)
describes the order in full. Removing a membership removes the rights that came through it.

## The actor

Every ledger entry records its **actor**, the subject on whose behalf it was made. `fulfill` records the `actor`
argument; without it, the only subject when `subjects` holds exactly one, otherwise no actor:

```python
await fulfill("edit_members", 1, {"user": user})                       # actor: user
await fulfill("send_email", 1, {"user": user, "team": team}, actor=user)
```

The actor is the subject the caller passed, never the group it expanded to: when the club's wallet pays for a member's
use, the entry is on the club's wallet with the member as actor. `credit` and `transfer` take `actor=` and store it as
given. `unfulfill(guid, actor=...)` records who reversed the entries on the storno entries; the originals keep theirs.
Why an entry was made (a reason, a ticket number) goes in its [metadata](/guide/concepts#transaction).

## The Django permissions backend

`allowances_django.permissions` holds Django permissions as rights.

A permission `"app_label.codename"` is the feature and the currency `"perm:app_label.codename"`;
`permission_feature("club.change_member")` returns that name. These need no configuration: the shortcuts' default
provider, `PermissionProvider(SettingsProvider())`, serves every `perm:` feature and currency that `settings.ALLOWANCES`
does not define — the currency at rate 0, the feature with the fulfilment order `[("grant", None)]`, so a wallet of any
subject passed, or of its groups, holds the permission.

```python
from allowances_django.permissions import grant_permission, permission_feature, revoke_permission

await grant_permission(board, "club.change_member")
await grant_permission(user, "club.view_report", valid_until=end_of_audit)
await revoke_permission(board, "club.change_member")   # returns how many wallets it closed
```

`grant_permission` opens a grant wallet in currency `perm:<perm>` with balance 1 and source `"permission"`, which no
`plan_switch` touches, from `valid_from` (default now) until `valid_until` (default: no end). `revoke_permission` closes
every live grant wallet of that subject in that currency that has not ended yet.
`allowances_django.sync.permissions` has both as synchronous functions.

A use of a permission goes on the ledger like any other right:

```python
result = await fulfill(permission_feature("club.change_member"), 1, {"user": user})
```

### Configuring the backend

```python
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allowances_django.permissions.AllowancesBackend",
]
```

`AllowancesBackend` authenticates nobody and answers permission checks. Alone, it is the only source of permissions;
chained after `ModelBackend`, Django grants a permission that either backend grants. `user.has_perm`, `{% if
perms.club.change_member %}`, `@permission_required`, `PermissionRequiredMixin`, `ModelAdmin.has_*_permission` and DRF's
permission classes all read it through `has_perm`.

The synchronous checks (`has_perm`, `get_all_permissions`, `has_module_perms`) run the
[synchronous API](/guide/synchronous) with the store of `ALLOWANCES_SYNC_STORE`. Django 5.2 and later calls
`ahas_perm`, `aget_all_permissions` and `ahas_module_perms` from asynchronous code (`await user.ahas_perm(...)`);
these run the asynchronous API with the store of `ALLOWANCES_STORE`. The backend's `get_sync_store()` and
`get_store()` return those stores; a subclass overrides them to use others.

`get_all_permissions(user)` is the permissions held by grant wallets of the user and of every group in `groups_of(user)`
that are valid now and have a positive balance: the keys of [`effective_permissions`](#effective-permissions) applied to
the user's grant allowances. An active superuser has every `Permission`; an inactive or anonymous user has none.
`has_perm` and `has_module_perms` answer from that set. The set is read once per user object and cached on it
(`_allowances_perm_cache`), as `ModelBackend` caches its own: a grant or revocation is seen by the next request's user
object. The synchronous and the asynchronous checks share that cache.

To give the members of an `auth.Group` a permission, make the user a member with `await add_member(user, group)` and
grant the permission to the group. Django's own `user.groups` is a separate table and does not count.

## Effective permissions

[`effective_allowances`](/guide/concepts#what-a-subject-holds) lists every wallet a subject holds, its own and its
groups', in the library's terms. `effective_permissions` is a filter over that list: it keeps the grant allowances in a
`perm:` currency with a positive balance and groups them by permission.

```python
from allowances_django.permissions import effective_permissions
from allowances_django.shortcuts import effective_allowances

effective_permissions(await effective_allowances(user))
# {
#     "club.change_member": [
#         Allowance(wallet=Wallet(id=12, ...), holder=SubjectRef("auth.user", "5"),
#                   depth=0, balance=Decimal("1.000000")),
#         Allowance(wallet=Wallet(id=7, ...), holder=SubjectRef("auth.group", "1"),
#                   depth=1, balance=Decimal("1.000000")),
#     ],
#     "club.view_member": [
#         Allowance(wallet=Wallet(id=9, ..., valid_until=datetime(2026, 12, 31, ...)),
#                   holder=SubjectRef("auth.group", "2"), depth=2, balance=Decimal("1.000000")),
#     ],
# }
```

The keys are the permissions `"app_label.codename"`, sorted. Each value lists the allowances conferring that permission
in the order `effective_allowances` returned them: nearest holder first. `holder` is the subject whose wallet confers it
and `depth` its distance from the queried subject — 0 for the subject itself, 1 for a direct group, 2 for a group of
that group. `wallet.valid_until` is when that grant ends.

`effective_allowances` takes any subject, so the same call answers what an `auth.Group`, an organisation or a device
holds. `effective_permissions` reads nothing itself and applies no superuser or `is_active` rule; those belong to
`AllowancesBackend`. The rest of the list — budgets, credits, rights outside Django permissions — is left to the caller:

```python
held = [a for a in await effective_allowances(user) if a.balance > 0]
```

## Limits

- **No object-level permissions.** `has_perm(user, perm, obj)` with an `obj` is always false from this backend.
- **No deny rules.** A permission is held or not; nothing subtracts one granted through a group.
- **No admin UI for granting.** The admin lists wallets and memberships; `grant_permission`, `revoke_permission`,
  `add_member` and `remove_member` are the API.
