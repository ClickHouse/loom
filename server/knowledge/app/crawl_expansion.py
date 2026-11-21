"""Complete bounded Workspace membership; no partial expansion becomes reader evidence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from server.auth import scim
from server.clients import gdrive
from server.clients import google_workspace as google
from server.clients._vendor import CredentialRefused, TooLarge, VendorError
from server.core.ch import set_query_tag
from server.knowledge.domain.source_crawl import MAX_PEOPLE, Expansion, Options, Person


class Standing(Protocol):
    async def validate(self) -> None: ...


class Exclusions(Protocol):
    async def version(self) -> str: ...

    async def contains(self, person: Person) -> bool: ...


@dataclass(frozen=True)
class Limits:
    body_bytes: int
    member_pages: int
    groups: int

    def __post_init__(self) -> None:
        if any(
            type(v) is not int or v <= 0 for v in (self.body_bytes, self.member_pages, self.groups)
        ):
            raise ValueError("positive crawl bounds required")


async def expand(
    options: Options,
    account: gdrive.ServiceAccount,
    *,
    standing: Standing,
    exclusions: Exclusions,
    limits: Limits,
    deadline: float,
) -> Expansion:
    """Resolve all selected people/groups under one source and exclusion observation."""
    set_query_tag("knowledge.crawl.expand")
    async with asyncio.timeout_at(deadline):
        observation = _Expansion(options, account, standing, exclusions, limits, deadline)
        return await observation.run()


class _Expansion:
    def __init__(
        self,
        options: Options,
        account: gdrive.ServiceAccount,
        standing: Standing,
        exclusions: Exclusions,
        limits: Limits,
        deadline: float,
    ) -> None:
        self.options, self.account = options, account
        self.standing, self.exclusions, self.limits = standing, exclusions, limits
        self.deadline = deadline
        self.version = ""
        self.customer = ""
        self.tokens: dict[gdrive.ReadScope, google.DelegatedToken] = {}
        self.people: dict[str, Person] = {}
        self.identities: dict[str, Person | None] = {}
        self.groups: dict[str, tuple[str, ...]] = {}
        self.group_names: dict[str, str] = {}
        self.visiting: set[str] = set()
        self.group_count = 0
        self.page_count = 0

    async def guard(self) -> None:
        await self.standing.validate()
        if await self.exclusions.version() != self.version:
            raise VendorError("crawl exclusions changed")

    async def token(self, scope: gdrive.ReadScope) -> google.DelegatedToken:
        if scope not in self.tokens:
            await self.guard()
            token = await google.delegated_token(
                self.account,
                subject=self.options.directory_subject,
                scope=scope,
                deadline=self.deadline,
                max_bytes=self.limits.body_bytes,
            )
            await self.guard()
            self.tokens[scope] = token
        return self.tokens[scope]

    async def run(self) -> Expansion:
        await self.standing.validate()
        self.version = await self.exclusions.version()
        if not self.version:
            raise VendorError("crawl exclusions unavailable")
        token = await self.token(gdrive.ReadScope.DIRECTORY_USERS)
        await self.guard()
        principal = await google.directory_identity(token, max_bytes=self.limits.body_bytes)
        await self.guard()
        self.customer = principal.customer_id
        for subject in self.options.subjects:
            if subject.kind == "person":
                await self.person(subject.email)
            else:
                await self.group(subject.email, subject.email)
        await self.guard()
        return Expansion(
            self.customer,
            tuple(self.people[k] for k in sorted(self.people)),
            tuple(sorted(self.groups.items())),
            self.version,
        )

    async def person(self, key: str) -> Person | None:
        if key in self.identities:
            return self.identities[key]
        token = await self.token(gdrive.ReadScope.DIRECTORY_USERS)
        await self.guard()
        found = await google.user(
            token, key, customer_id=self.customer, max_bytes=self.limits.body_bytes
        )
        await self.guard()
        person = Person(
            found.customer_id, found.id, found.email.lower(), tuple(sorted(set(found.aliases)))
        )
        old = self.people.get(person.id)
        if old is not None and old != person:
            raise VendorError("crawl person changed")
        denied = await self.exclusions.contains(person)
        for address in person.addresses:
            denied = await scim.deactivated(address) or denied
        await self.guard()
        admitted = None if denied else person
        for spelling in (person.id, *person.addresses):
            if spelling in self.identities and self.identities[spelling] != admitted:
                raise VendorError("crawl identity is ambiguous")
            self.identities[spelling] = admitted
        if admitted is not None:
            self.people[person.id] = person
            if len(self.people) > MAX_PEOPLE:
                raise TooLarge("crawl exceeds its people bound")
        return admitted

    async def before_page(self) -> None:
        await self.guard()
        if self.page_count >= self.limits.member_pages:
            raise TooLarge("crawl exceeds its directory page bound")
        self.page_count += 1

    async def group(self, key: str, name: str) -> tuple[str, ...]:
        canonical = self.group_names.get(name, key)
        if canonical in self.visiting:
            raise VendorError("crawl group cycle")
        if name in self.groups:
            return self.groups[name]
        self.group_count += 1
        if self.group_count > self.limits.groups:
            raise TooLarge("crawl exceeds its group bound")
        self.visiting.add(canonical)
        self.group_names[name] = canonical
        token = await self.token(gdrive.ReadScope.DIRECTORY_MEMBERS)
        await self.guard()
        members = await google.group_members(
            token,
            key,
            max_bytes=self.limits.body_bytes,
            max_pages=self.limits.member_pages,
            before_page=self.before_page,
        )
        await self.guard()
        found: set[str] = set()
        for member in members:
            if member.kind == "CUSTOMER":
                raise CredentialRefused("customer-wide crawl membership refused")
            if member.kind == "GROUP":
                if member.email is None:
                    raise VendorError("crawl group address unavailable")
                nested = await self.group(member.id, member.email)
                found.update(nested)
            else:
                person = await self.person(member.id)
                if person is not None:
                    found.add(person.email)
        await self.guard()
        self.visiting.remove(canonical)
        self.groups[name] = tuple(sorted(found))
        return self.groups[name]
