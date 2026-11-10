"""collection.create, collection.move, collection.list and collection.delete,
and the retirement a delete ends with (server/auth/sharing.py holds the rules
they keep)."""

from __future__ import annotations

from typing import Any

from server.auth import sharing
from server.auth.authz import check_authorized
from server.auth.context import current_auth
from server.auth.grant_claims import account_change, collections_change
from server.core import grant_cache
from server.core.ch import get_client, own_write_reads
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import AuthInsufficientError, CollectionNameTakenError, ValidationError
from server.core.naming import validate_namespace_name
from server.core.principals import COLLECTION_PREFIX, PERSONAL_PREFIX
from server.identity.api.models import NamespaceRegisterInput
from server.identity.api.models_collections import (
    CollectionCreateInput,
    CollectionCreateOutput,
    CollectionDeleteInput,
    CollectionDeleteOutput,
    CollectionListInput,
    CollectionListOutput,
    CollectionMoveInput,
    CollectionMoveOutput,
    _CollectionEntry,
)
from server.identity.app import registration
from server.identity.app.namespace import (
    delete_namespace,
    insert_admin_grant,
    registry_row,
    resolve_subject,
)
from server.identity.domain import collection_names
from server.identity.store import collections as collections_repo
from server.identity.store import grants as grants_repo
from server.identity.store import namespaces as namespaces_repo
from server.operations.base import Role


async def _may(required_role: Role, org: str, namespace: str) -> bool:
    try:
        await check_authorized(current_auth(), required_role, org, namespace)
    except PermissionError:
        return False
    return True


def check_placement(tree: sharing.Tree, child: str, parent: str) -> None:
    """ValidationError unless `child` may sit under `parent`: a collection that
    is not personal, not `child` or below it, and leaving no collection with
    more than MAX_DEPTH ancestors."""
    if sharing.is_personal(parent):
        raise ValidationError("a personal collection holds no other collection")
    if parent not in tree.parent:
        raise ValidationError(f"{parent!r} is not a collection")
    lineage = tree.lineage(parent)
    if child in lineage:
        raise ValidationError(f"{parent!r} is inside {child!r}; a collection cannot hold itself")
    if len(lineage) + tree.height(child) > sharing.MAX_DEPTH:
        raise ValidationError(f"collections nest at most {sharing.MAX_DEPTH} deep")


def _check_name(namespace: str) -> None:
    try:
        validate_namespace_name(namespace)
    except ValueError as e:
        raise ValidationError(str(e)) from e
    if sharing.is_personal(namespace):
        raise ValidationError(
            f"the prefix {PERSONAL_PREFIX!r} is reserved for personal collections"
        )
    if not sharing.is_collection(namespace):
        raise ValidationError(f"a collection's name starts with {COLLECTION_PREFIX!r}")


# What a name is to a create by `caller` under `parent`: free (or a create of the
# caller's own that stopped before its directory row, which this one finishes),
# the caller's own collection there, or taken (in use, or a deleted collection's).
_FREE, _MINE, _USED, _DELETED = "free", "mine", "used", "deleted"


async def _name_state(org: str, namespace: str, parent: str, caller: str) -> str:
    with own_write_reads():  # a create on another replica a moment ago
        directory_row = await namespaces_repo.fetch_row(org, namespace)
    current = await collections_repo.fetch(org, namespace)
    if current is None and directory_row is not None:
        gone = directory_row.get("kind") == "collection" and not directory_row.get("active")
        return _DELETED if gone else _USED  # a deleted collection, or some other namespace
    # A collection whose delete stopped before the tree (its directory row inactive) is a
    # deleted one, whoever made it.
    if current is not None and directory_row is not None and not directory_row.get("active"):
        return _DELETED
    if current is not None and current["owner"] != caller:
        return _USED
    # The caller's own collection, made whole: whoever it was shared with since.
    if current is not None and directory_row is not None:
        return _MINE if current["parent"] == parent else _USED
    # A grant someone else holds on the name would open the new collection.
    if any(g["subject"] != caller for g in await grants_repo.list_active(org, namespace)):
        return _USED
    # Data under the name with no directory row would join the new collection.
    if current is None and await registration.refused_as_in_use(org, namespace):
        return _USED
    return _FREE


async def _write_new(input: CollectionCreateInput, namespace: str, caller: str) -> None:
    """The tree row and the creator's grant first, the directory row last: a
    create cut short is finished by its creator's retry, and the name is never
    left with no admin."""
    org, parent = input.org, input.parent
    if parent:
        check_placement(await sharing.load_tree(org), namespace, parent)
    ch = get_client()
    await collections_repo.write(
        collections_repo.collection_row(
            org,
            namespace,
            parent=parent,
            owner=caller,
            after=await collections_repo.stored_version(org, namespace),
        ),
        ch=ch,
    )
    await grant_cache.changed(org, kind=grant_cache.COLLECTIONS)
    await insert_admin_grant(ch, org, namespace, caller)
    row = registry_row(
        NamespaceRegisterInput(
            org=org, namespace=namespace, owner_email=caller, description=input.description
        ),
        None,
        naive_utc_now_seconds(),
    )
    row["kind"] = "collection"
    row["title"] = input.title
    await ch.insert("loom.namespaces", [row])


async def _give_title(org: str, namespace: str, title: str) -> str:
    """The collection's title: its own, else `title`, stored on it then when the
    caller is its admin (it was made with none, by a build without titles or by
    name alone). The caller holds the name's account claim."""
    with own_write_reads():
        row = await namespaces_repo.fetch_row(org, namespace)
    if row is None or row.get("title") or not title:
        return str((row or {}).get("title") or "")
    if not await _may(Role.ADMIN, org, namespace):
        return ""  # what everyone who reads it sees is its admin's to name
    # Never behind the row it replaces (on a tie the row inserted last wins); a delete a
    # moment later stamps the same way, so it still wins.
    stamp = max(naive_utc_now_seconds(), row["updated_at"])
    await get_client().insert("loom.namespaces", [{**row, "title": title, "updated_at": stamp}])
    return title


def _title_names(title: str, own: str, name: str, held: str) -> bool:
    """Whether `title` names the collection `name` whose title is `own`: the
    same title, or none and `name` is `held`, a name `title` spells."""
    if own:
        return collection_names.title_key(own) == collection_names.title_key(title)
    return name == held and collection_names.spells(title)


def _token_writes(org: str, namespace: str) -> bool:
    """A token that writes only its `ns_write` set is handed nothing outside it."""
    auth = current_auth()
    return auth is None or not auth.write_namespaces or auth.can_write(org, namespace)


async def _titled_sibling(
    org: str, parent: str, title: str, caller: str, held: str
) -> dict[str, Any] | None:
    """The collection `title` already names under `parent`: a child whose title
    matches, or an untitled child named `held` (the title's plain name). At the
    top level, only the caller's own; under a parent, any child (the caller
    writes the parent). The caller holds the org's tree claim."""
    kids = await collections_repo.children(org, parent, owner="" if parent else caller)
    with own_write_reads():
        labels = await namespaces_repo.labels(org, [k["namespace"] for k in kids])
    for kid in sorted(kids, key=lambda k: k["namespace"]):
        name = kid["namespace"]
        own = labels.get(name, {}).get("title", "")
        if not _title_names(title, own, name, held) or not _token_writes(org, name):
            continue
        # A tree row with no directory row is a create cut short, not a collection.
        with own_write_reads():  # a create on another replica a moment ago
            row = await namespaces_repo.fetch_row(org, name)
        if row is None or not row.get("active"):
            continue
        # Under a parent the caller writes, every child inherits that grant; at the top
        # level the creator may have lost theirs since.
        if not parent and not await _may(Role.WRITER, org, name):
            continue
        return {"namespace": name, "title": own}
    return None


async def _held_by_another(org: str, namespace: str, caller: str) -> bool:
    """A cheap read, without claims: the name's tree or directory row is someone
    else's, so it is not worth claiming."""
    current = await collections_repo.fetch(org, namespace)
    if current is not None:
        return bool(current["owner"] != caller)
    return await namespaces_repo.fetch_row(org, namespace) is not None


async def _suggestion(org: str, namespace: str, parent: str, caller: str) -> str:
    """A free name like `namespace`, or ''. Read without claims: a hint."""
    base = namespace.removeprefix(COLLECTION_PREFIX)
    for name in collection_names.name_candidates(org, caller, parent, base):
        if name == namespace or not _token_writes(org, name):
            continue
        if await _name_state(org, name, parent, caller) == _FREE:
            return name
    return ""


async def handle_collection_create(input: CollectionCreateInput) -> CollectionCreateOutput:
    org, parent = input.org, input.parent
    caller = resolve_subject()
    if parent and not await _may(Role.WRITER, org, parent):
        raise AuthInsufficientError(f"{caller!r} cannot add a collection to {org}/{parent}")
    if input.derive_name:
        return await _create_titled(input, caller)
    return await _create_named(input, caller)


async def _create_named(input: CollectionCreateInput, caller: str) -> CollectionCreateOutput:
    org, namespace, parent = input.org, input.namespace, input.parent
    _check_name(namespace)
    if not _token_writes(org, namespace):
        raise AuthInsufficientError(f"this token may not write {org}/{namespace}")
    async with account_change(org, namespace), collections_change(org):
        state = await _name_state(org, namespace, parent, caller)
        if state == _MINE:
            title = await _give_title(org, namespace, input.title)
            return CollectionCreateOutput(
                namespace=namespace, parent=parent, created=False, title=title
            )
        if state == _FREE:
            await _write_new(input, namespace, caller)
            return CollectionCreateOutput(
                namespace=namespace, parent=parent, created=True, title=input.title
            )
    suggestion = await _suggestion(org, namespace, parent, caller)
    if state == _DELETED:
        raise CollectionNameTakenError(
            f"the name {namespace!r} belonged to a deleted collection in {org}: a deleted "
            "name is never used again, so its old grants, audit rows and links cannot reach "
            "new content",
            deleted=True,
            suggestion=suggestion,
        )
    raise CollectionNameTakenError(
        f"the name {namespace!r} is already used in {org}", suggestion=suggestion
    )


async def _create_titled(input: CollectionCreateInput, caller: str) -> CollectionCreateOutput:
    """The first name the title derives that is free, unless the title already
    names a collection there (`_titled_sibling`), which answers instead."""
    org, parent, title = input.org, input.parent, input.title
    every = list(collection_names.name_candidates(org, caller, parent, title))
    # The name the title spells; an untitled collection by it is the title's.
    plain = every[0]
    names = [n for n in every if _token_writes(org, n)]
    if not names:  # a token scoped by ns_write lists none of them
        raise AuthInsufficientError(
            f"this token writes only the namespaces it lists in {org}; give the collection "
            "one of those as its name"
        )
    for i, namespace in enumerate(names):
        # The first name tried is always claimed: an untitled collection there
        # may be the title's (`_titled_sibling`).
        if i and await _held_by_another(org, namespace, caller):
            continue
        async with account_change(org, namespace), collections_change(org):
            # Under the tree claim, so a second create of the title finds the first.
            same = await _titled_sibling(org, parent, title, caller, plain)
            if same is not None:
                shown = same["title"]
                # Titled only under its own account claim, which is held when it is the name tried.
                if not shown and same["namespace"] == namespace:
                    shown = await _give_title(org, namespace, title)
                return CollectionCreateOutput(
                    namespace=same["namespace"], parent=parent, created=False, title=shown
                )
            state = await _name_state(org, namespace, parent, caller)
            if state == _FREE:
                await _write_new(input, namespace, caller)
                return CollectionCreateOutput(
                    namespace=namespace, parent=parent, created=True, title=title
                )
            if state == _MINE:
                # The caller's own collection here, which the sibling read may have
                # missed: the title's when its title (or, untitled, its name) says so.
                with own_write_reads():
                    row = await namespaces_repo.fetch_row(org, namespace)
                own = str((row or {}).get("title") or "")
                writes = await _may(Role.WRITER, org, namespace)  # a revoked creator does not
                if writes and _title_names(title, own, namespace, plain):
                    shown = own or await _give_title(org, namespace, title)
                    return CollectionCreateOutput(
                        namespace=namespace, parent=parent, created=False, title=shown
                    )
    raise CollectionNameTakenError(
        f"no free name derives from the title {title!r} in {org}; give the collection a name"
    )


def create_audit_namespace(_input: CollectionCreateInput, response: Any) -> str:
    """A create's audit row is the collection's, whatever name the request sent."""
    return str(response["namespace"])


def create_audit_subject(input: CollectionCreateInput, response: Any) -> dict[str, Any]:
    """The collection a create made or found, named even when only its title was sent."""
    return {
        "org": input.org,
        "namespace": response["namespace"],
        "parent": input.parent,
        "created": response["created"],
    }


async def handle_collection_move(input: CollectionMoveInput) -> CollectionMoveOutput:
    """The transport checked admin on the collection; writer on the new parent here."""
    org, namespace, parent = input.org, input.namespace, input.parent
    caller = resolve_subject()
    if parent and not await _may(Role.WRITER, org, parent):
        raise AuthInsufficientError(f"{caller!r} cannot add a collection to {org}/{parent}")
    async with collections_change(org):
        current = await collections_repo.fetch(org, namespace)
        if current is None:
            raise ValidationError(f"{namespace!r} is not a collection that can move")
        if current["parent"] == parent:
            return CollectionMoveOutput(namespace=namespace, parent=parent, moved=False)
        if parent:
            check_placement(await sharing.load_tree(org), namespace, parent)
        row = collections_repo.collection_row(
            org,
            namespace,
            parent=parent,
            owner=current["owner"],
            created_at=current["created_at"],
            after=current["version"],
        )
        async with grant_cache.announced(org, kind=grant_cache.COLLECTIONS):
            await collections_repo.write(row)
    return CollectionMoveOutput(namespace=namespace, parent=parent, moved=True)


async def handle_collection_list(input: CollectionListInput) -> CollectionListOutput:
    """The collections the caller can read, by name, one page at a time."""
    auth = current_auth()
    tree = await sharing.tree(input.org)
    names = await sharing.readable(auth, input.org) if auth is not None else sorted(tree.parent)
    names = [ns for ns in names if ns > (input.cursor or "")]
    page = names[: input.limit]
    if auth is None:
        roles: dict[str, Role | None] = dict.fromkeys(page, Role.ADMIN)
    else:
        roles = await sharing.roles(auth, input.org, page)
    with own_write_reads():  # a collection created on another replica a moment ago
        labels = await namespaces_repo.labels(
            input.org, [ns for ns in page if roles.get(ns) is not None]
        )
    entries = [
        _CollectionEntry(
            namespace=ns,
            parent=tree.parent.get(ns, ""),
            personal=sharing.is_personal(ns),
            role=role.name.lower(),
            description=labels.get(ns, {}).get("description", ""),
            title=labels.get(ns, {}).get("title", ""),
        )
        for ns in page
        if (role := roles.get(ns)) is not None
    ]
    more = len(names) > input.limit
    return CollectionListOutput(collections=entries, next_cursor=page[-1] if more else None)


async def handle_collection_delete(input: CollectionDeleteInput) -> CollectionDeleteOutput:
    """Delete a collection (the transport checked admin on it) with what it
    holds: a purging erasure of the namespace first, while its admin still
    reaches it (a saga that fails is retried by the recovery worker), then it
    leaves the directory and the tree, and its children become top-level. A personal
    collection goes with its user's erasure."""
    from server.erasure.app.handlers import erase_namespace

    if sharing.is_personal(input.namespace) or not sharing.is_collection(input.namespace):
        raise ValidationError(f"{input.namespace!r} is not a collection that can be deleted")
    erasure = await erase_namespace(
        input.org, input.namespace, requester=resolve_subject() or "collection.delete"
    )
    out = await delete_namespace(input.org, input.namespace)
    return CollectionDeleteOutput(
        namespace=input.namespace,
        deleted=out.deleted,
        erasure_id=erasure.erasure_id,
        erasure_status=erasure.status,
    )


async def retire(org: str, namespace: str) -> bool:
    """A deleted collection leaves the tree; its children become top-level,
    keeping their own grants. False when it was not in the tree."""
    async with collections_change(org):
        current = await collections_repo.fetch(org, namespace)
        if current is None:
            return False
        async with grant_cache.announced(org, kind=grant_cache.COLLECTIONS):
            await _retire_rows(org, namespace, current)
    return True


async def _retire_rows(org: str, namespace: str, current: dict[str, Any]) -> None:
    """The children become top-level, then the collection's own row is deleted."""
    for child in await collections_repo.children(org, namespace):
        await collections_repo.write(
            collections_repo.collection_row(
                org,
                child["namespace"],
                owner=child["owner"],
                created_at=child["created_at"],
                after=child["version"],
            )
        )
    await collections_repo.write(
        collections_repo.collection_row(
            org,
            namespace,
            parent=current["parent"],
            owner=current["owner"],
            created_at=current["created_at"],
            deleted=True,
            after=current["version"],
        )
    )
