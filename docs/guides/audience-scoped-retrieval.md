# Choose retrieval scope from the audience

Needs an end-user issuer (`LOOM_JWT_END_USER_ISSUERS_JSON`): an agent host signs each
request as the person who asked, and picks the collections a reply may draw on from who
will read it. A person may read their own notes, and a reply everyone in a shared
conversation sees may still not draw on them. This guide gives the scope rule, the
checks Loom makes, and the ones that stay with the host.

The pattern is runnable:
[`sdk/python/examples/audience_scoped_retrieval.py`](../../sdk/python/examples/audience_scoped_retrieval.py)
asks one question in a private conversation and in a shared one, and checks the context
each reply gets without calling a model.

## 1. Resolve who asks and where the reply goes

| Part                | In the example                                                  | Who decides it                                            |
| ------------------- | --------------------------------------------------------------- | --------------------------------------------------------- |
| Requesting person   | `user:<id>`, the `sub` of an end-user token                     | The host's authenticated session, signed into the token   |
| Executing principal | The same `user:<id>`: every Loom call of a request runs as them | The host, which holds the signing key                     |
| Permitted sources   | The collections the person holds a grant on, and `@me`          | Loom, from grants ([sharing](../architecture/sharing.md)) |
| Destination         | A private conversation, or a shared one with its members        | The host's own records                                    |

The agent is none of these. It holds no token and names no collection.

## 2. Intersect the destination's sources with the person's

A reply may draw on a collection when both hold:

1. The destination's readers may all see it. The host keeps, for each destination, its
   members and the collections they share. A private conversation has one member, so
   everything that person reads is in scope, their personal collection included.
2. The requesting person may read it. The host asks Loom with `collection.list`, called
   as the person.

The host passes the result to `knowledge.search` as `namespace` and `namespaces`, which
search those collections and no others. It never sends `namespace_scope: "all_mine"`
for a shared destination: that widens the search to everything the person reads.

```python
async def scope(self, person: str, destination: str) -> list[str]:
    where = self.destinations.get(destination)
    if where is None or person not in where.members:
        return []
    ...  # `readable`: collection.list as the person
    if where.private:
        return sorted(readable)
    return [ns for ns in where.sources if ns in readable and not readable[ns]]
```

A destination the host cannot resolve, or one it does not know the person to be in,
gets an empty scope and no search.

## 3. Bind the agent's tools to that scope

The host exposes two tools, bound to one conversation. Their arguments are the whole
surface a prompt can reach:

| Tool            | Argument | Does                                                                                          |
| --------------- | -------- | --------------------------------------------------------------------------------------------- |
| `search`        | `query`  | `knowledge.search` over the conversation's scope; each passage comes back with a handle       |
| `open_citation` | `handle` | `knowledge.get` of a passage this conversation retrieved, if its collection is still in scope |

A call with any other tool name or argument is refused before a request is made. So a
model cannot ask for `namespaces: ["@me"]`, `namespace_scope: "all_mine"`, a `user_id`,
a `source_uri` in another collection, or a citation of another conversation. The
wording of a query changes nothing: the scope is computed before the query is read.

Any other op the host exposes (`knowledge.list`, `search.query`, `memory.search`) needs
the same treatment: bind the person and the scope in the host, and take only the query
from the model.

## 4. Keep each conversation's state apart and check delivery

Reusing one agent for both conversations is safe only if nothing retrieved for one
reaches the other. The host keeps one state per person and destination: the passages
it retrieved and the handles it issued. Summaries and cached answers built from that
context belong in the same state, never in a store keyed by the person alone.

Before a reply leaves, the host compares its target with the destination the context
was chosen for and refuses a mismatch. A reply moved to another destination is a new
request with a new scope.

## What Loom enforces and what the host does

| Check                                                                           | Enforced by                                                  |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| The token names one person; no header or body field names another               | Loom ([identity](../architecture/sharing.md#identity))       |
| Each collection a search names is one the person may read, else the call is 403 | Loom                                                         |
| A personal collection opens to its own user only                                | Loom                                                         |
| A revoked grant or a removed group member stops reading within the grant poll   | Loom ([consistency](../architecture/sharing.md#consistency)) |
| Which collections a destination's readers share, and who is in the destination  | Host                                                         |
| A shared request names the destination's collections instead of a wider search  | Host                                                         |
| The agent's tool arguments cannot name a collection, a user or a search scope   | Host                                                         |
| Retrieved text stays in the conversation it was retrieved for                   | Host                                                         |
| A reply goes to the destination its context was chosen for                      | Host                                                         |

Loom has no principal for "who will read the answer". The second half of the table is
a convention of the host, not a disclosure guarantee of the server: see
[Limits](#limits).

## What the example checks

1. The private request gets the person's note and both shared collections.
2. The shared request gets the destination's collection, not the note and not the other
   team's collection.
3. Six tool calls that try to widen the scope are refused, a query that asks for
   private notes changes nothing, and a private reply is not delivered to the shared
   conversation.
4. An unknown destination, and one the person is not known to be in, get no passage.
5. After `grants.revoke`, the shared request gets no passage and its earlier citation
   no longer opens.

`tests/unit/test_sdk_example_audience_scoped_retrieval.py` runs them against a scripted
server, and `tests/integration/test_sdk_example_audience_scoped_retrieval.py` against a
live one.

## Limits

- **An end-user token cannot be held to a destination.** It carries no namespace claim
  ([what it may name](../architecture/sharing.md#identity)), so a token signed for a
  shared request reads everything its person reads, `@me` included. The narrowing is
  the host's choice of `namespaces` on each call. Keep the token and the signing key in
  the host: a tool environment that holds either one is not bound by this pattern. A
  claim that caps an end-user token at named collections would move this check into
  Loom; it is not part of the wire today.
- **A scoped Loom token is held to its collections, for a different identity.** A token
  minted with `loom mint-token --namespace-role <collection>=viewer` reads a collection
  only when its claim names it and its subject holds a grant there: a call that names
  another collection the subject holds a grant on is 403. A host that must hand a
  credential to the agent can mint one per destination for a service account granted
  that destination's collections. Loom then enforces the destination's sources, and the
  check that the requesting person may read them moves to the host.
- **A revoke is not instant everywhere.** The server process that takes the revoke
  enforces it at once; another process within `LOOM_ACL_WATERMARK_POLL_SECONDS`.
- **Membership is the host's.** Loom does not know who is in a conversation. When the
  host cannot confirm it, the request gets no context.

## Related

- [Sharing knowledge](../architecture/sharing.md): collections, grants, end-user tokens.
- [Act for a service's end users](tokens-and-grants.md#act-for-a-services-end-users):
  registering the issuer and signing a token.
- [Knowledge](../features/knowledge.md#searching): `knowledge.search` and its scope fields.
