"""collection.create, collection.move and collection.list (server/auth/sharing.py
holds the rules they keep)."""

from __future__ import annotations

from server.auth import sharing
from server.auth.authz import check_authorized
from server.auth.context import current_auth
from server.auth.grant_claims import account_change, collections_change
from server.core import grant_cache
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import AuthInsufficientError, PreconditionFailedError, ValidationError
from server.core.naming import validate_namespace_name
from server.identity.api.models import NamespaceRegisterInput
from server.identity.api.models_collections import (
    CollectionCreateInput,
    CollectionCreateOutput,
    CollectionListInput,
    CollectionListOutput,
    CollectionMoveInput,
    CollectionMoveOutput,
    _CollectionEntry,
)
from server.identity.app.namespace import insert_admin_grant, registry_row, resolve_subject
from server.identity.store import collections as collections_repo
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
    if parent not in tree.parent:
        raise ValidationError(f"{parent!r} is not a collection")
    if parent in tree.personal:
        raise ValidationError("a personal collection holds no other collection")
    lineage = tree.lineage(parent)
    if child in lineage:
        raise ValidationError(f"{parent!r} is inside {child!r}; a collection cannot hold itself")
    if len(lineage) + tree.height(child) > sharing.MAX_DEPTH:
        raise ValidationError(f"collections nest at most {sharing.MAX_DEPTH} deep")


def _check_name(namespace: str) -> None:
    validate_namespace_name(namespace)
    if not sharing.is_collection(namespace) or sharing.is_personal(namespace):
        raise ValidationError(f"a collection's name starts with {sharing.COLLECTION_PREFIX!r}")


async def handle_collection_create(input: CollectionCreateInput) -> CollectionCreateOutput:
    org, namespace, parent = input.org, input.namespace, input.parent
    _check_name(namespace)
    caller = resolve_subject()
    if parent and not await _may(Role.WRITER, org, parent):
        raise AuthInsufficientError(f"{caller!r} cannot add a collection to {org}/{parent}")
    async with collections_change(org), account_change(org, namespace):
        if await namespaces_repo.fetch_row(org, namespace) is not None:
            current = await collections_repo.fetch(org, namespace)
            if current is not None and current["parent"] == parent:
                if await _may(Role.ADMIN, org, namespace):
                    return CollectionCreateOutput(namespace=namespace, parent=parent, created=False)
            raise PreconditionFailedError(f"the name {namespace!r} is taken in {org}")
        if parent:
            check_placement(await sharing.load_tree(org), namespace, parent)
        ch = get_client()
        row = registry_row(
            NamespaceRegisterInput(
                org=org, namespace=namespace, owner_email=caller, description=input.description
            ),
            None,
            naive_utc_now_seconds(),
        )
        row["kind"] = "collection"
        await ch.insert("loom.namespaces", [row])
        await collections_repo.write(
            collections_repo.collection_row(org, namespace, parent=parent, owner=caller), ch=ch
        )
        # The grant write moves the org's watermark, so every process reads the
        # new tree with it.
        await insert_admin_grant(ch, org, namespace, caller)
    return CollectionCreateOutput(namespace=namespace, parent=parent, created=True)


async def handle_collection_move(input: CollectionMoveInput) -> CollectionMoveOutput:
    org, namespace, parent = input.org, input.namespace, input.parent
    caller = resolve_subject()
    if not await _may(Role.ADMIN, org, namespace):
        raise AuthInsufficientError(f"{caller!r} cannot move {org}/{namespace}")
    if parent and not await _may(Role.WRITER, org, parent):
        raise AuthInsufficientError(f"{caller!r} cannot add a collection to {org}/{parent}")
    async with collections_change(org):
        current = await collections_repo.fetch(org, namespace)
        if current is None or current["personal"]:
            raise ValidationError(f"{namespace!r} is not a collection that can move")
        if current["parent"] == parent:
            return CollectionMoveOutput(namespace=namespace, parent=parent, moved=False)
        if parent:
            check_placement(await sharing.load_tree(org), namespace, parent)
        await collections_repo.write(
            collections_repo.collection_row(
                org,
                namespace,
                parent=parent,
                owner=current["owner"],
                created_at=current["created_at"],
            )
        )
    await grant_cache.changed(org)
    return CollectionMoveOutput(namespace=namespace, parent=parent, moved=True)


async def handle_collection_list(input: CollectionListInput) -> CollectionListOutput:
    auth = current_auth()
    tree = await sharing.tree(input.org)
    names = await sharing.readable(auth, input.org) if auth is not None else sorted(tree.parent)
    entries = []
    for ns in names:
        role = await sharing.role(auth, input.org, ns) if auth is not None else Role.ADMIN
        entries.append(
            _CollectionEntry(
                namespace=ns,
                parent=tree.parent.get(ns, ""),
                personal=ns in tree.personal,
                role=(role or Role.VIEWER).name.lower(),
            )
        )
    return CollectionListOutput(collections=entries)


async def retire(org: str, namespace: str) -> None:
    """A deleted collection leaves the tree; its children become top-level,
    keeping their own grants."""
    async with collections_change(org):
        current = await collections_repo.fetch(org, namespace)
        if current is None:
            return
        for child in await collections_repo.children(org, namespace):
            await collections_repo.write(
                collections_repo.collection_row(
                    org,
                    child["namespace"],
                    owner=child["owner"],
                    created_at=child["created_at"],
                )
            )
        await collections_repo.write(
            collections_repo.collection_row(
                org,
                namespace,
                parent=current["parent"],
                personal=current["personal"],
                owner=current["owner"],
                created_at=current["created_at"],
                deleted=True,
            )
        )
    await grant_cache.changed(org)
