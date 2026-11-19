"""Own-only connection operations, independent of namespace administration."""

from typing import Literal, cast

from server.auth.context import current_auth
from server.core.secrets_at_rest import unseal
from server.knowledge.api.models_connections import (
    ConnectionDisconnectInput,
    ConnectionDisconnectOutput,
    ConnectionEntry,
    ConnectionListInput,
    ConnectionListOutput,
)
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.domain.connected_accounts import EMAIL_PURPOSE, KINDS
from server.knowledge.store import connected_accounts as store
from server.settings import settings


async def handle_connection_list(input: ConnectionListInput) -> ConnectionListOutput:
    org, subject = await accounts.own_subject(current_auth())
    if org != input.org:
        raise accounts.unavailable()
    found = {account.kind: account for account in await store.owned([subject], org=org)}
    entries = []
    for kind in sorted(KINDS):
        account = found.get(kind)
        connected = account is not None and await accounts.available(account)
        entries.append(
            ConnectionEntry(
                kind=cast(Literal["gdrive", "notion", "linear"], kind),
                connected=connected,
                account_email=(unseal(EMAIL_PURPOSE, account.account_email_enc, warn=False) or None)
                if connected and account is not None
                else None,
                scopes=list(account.scopes) if connected and account is not None else [],
                connect_url=f"{settings.public_base_url.rstrip('/')}/settings/connected-accounts?kind={kind}",
            )
        )
    return ConnectionListOutput(connections=entries)


async def handle_connection_disconnect(
    input: ConnectionDisconnectInput,
) -> ConnectionDisconnectOutput:
    org, subject = await accounts.own_subject(current_auth())
    if org != input.org:
        raise accounts.unavailable()
    await accounts.disconnect(org, subject, input.kind)
    return ConnectionDisconnectOutput(disconnected=True)
