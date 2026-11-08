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
from server.core.errors import AuthInsufficientError, PreconditionFailedError, ValidationError
from server.core.naming import validate_namespace_name
from server.core.principals import PERSONAL_PREFIX
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
        raise ValidationError(f"a collection's name starts with {sharing.COLLECTION_PREFIX!r}")


async def handle_collection_create(input: CollectionCreateInput) -> CollectionCreateOutput:
    org, namespace, parent = input.org, input.namespace, input.parent
    _check_name(namespace)
    caller = resolve_subject()
    auth = current_auth()
    if auth is not None and auth.write_namespaces and not auth.can_write(org, namespace):
        # A token that writes only its `ns_write` set creates nothing outside it.
        raise AuthInsufficientError(f"this token may not write {org}/{namespace}")
    if parent and not await _may(Role.WRITER, org, parent):
        raise AuthInsufficientError(f"{caller!r} cannot add a collection to {org}/{parent}")
    taken = PreconditionFailedError(f"the name {namespace!r} is taken in {org}")
    async with account_change(org, namespace), collections_change(org):
        with own_write_reads():  # a create on another replica a moment ago
            directory_row = await namespaces_repo.fetch_row(org, namespace)
        current = await collections_repo.fetch(org, namespace)
        if current is None and directory_row is not None:
            raise taken  # some other namespace
        if current is not None and current["owner"] != caller:
            raise taken
        # A grant someone else holds on the name would open the new collection.
        others = [
            g for g in await grants_repo.list_active(org, namespace) if g["subject"] != caller
        ]
        if others:
            raise taken
        # Data under the name with no directory row would join the new collection.
        if current is None and await registration.refused_as_in_use(org, namespace):
            raise taken
        if current is not None and directory_row is not None:
            if current["parent"] != parent:
                raise taken
            return CollectionCreateOutput(namespace=namespace, parent=parent, created=False)
        # The tree row and the creator's grant first, the directory row last:
        # a create cut short is finished by its creator's retry, and the name
        # is never left with no admin.
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
        await ch.insert("loom.namespaces", [row])
    return CollectionCreateOutput(namespace=namespace, parent=parent, created=True)


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
        described = await namespaces_repo.descriptions(
            input.org, [ns for ns in page if roles.get(ns) is not None]
        )
    entries = [
        _CollectionEntry(
            namespace=ns,
            parent=tree.parent.get(ns, ""),
            personal=sharing.is_personal(ns),
            role=role.name.lower(),
            description=described.get(ns, ""),
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
