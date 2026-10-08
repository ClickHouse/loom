"""loom as a MemoryArena memory system.

Two modes behind the gym's two-call contract (add_chunk / wrap_user_prompt):
  verbatim — every chunk is stored as-is with memory.set, ONE memory per
             subtask (split only past 7,000 tokens, the embedder's limit;
             a 2,048 split like the RAG baseline's cuts derivations
             mid-formula), retrieved with loom's hybrid
             memory.search and returned whole via bodies=unit.
  extract  — chunks go through memory.set_from_messages (loom's extractor
             writes facts) and are retrieved the same way.
One fresh namespace per gym user_id (= task). Every call's latency is
appended to LOOM_MA_LATENCY_LOG for the seconds-per-subtask report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

import requests
import tiktoken


class LoomMemorySystem:
    def __init__(
        self,
        user_id: str,
        mode: str = "verbatim",
        top_k: int = 3,
        max_tokens: int = 7000,
        bodies: str = "unit",
        body_budget_tokens: int = 6000,
        search_mode: str = "rrf",
        recent_first: int = 0,
        min_body_chars: int = 0,
        # "" = server default (all_mine); "this" = the addressed namespace only
        namespace_scope: str = "",
        # one namespace for every task, tasks separated by run_id
        shared_namespace: str = "",
        # tag each trace with its traveler; filter reads by the names the query cites
        name_tags: bool = False,
        # write the traveler as the memory entity; the server does the addressing
        write_entities: bool = False,
        # server tier: the run's oldest memories first
        anchor_first: int = 0,
        # write each trace under its traveler's user_id, so the extractor's
        # dedup context is that traveler's own memories;
        # reads stay namespace-wide (read_scope=namespace is the default)
        actor_ids: bool = False,
        # bodies="source" capsule size in characters; 0 = whole passage
        source_span_chars: int = 0,
        ledger: bool = False,
        ledger_claim_chars: int = 500,
    ):
        self.user_id = str(user_id)
        self.mode = mode
        self.top_k = top_k
        self.max_tokens = max_tokens
        self.bodies = bodies
        self.body_budget_tokens = body_budget_tokens
        self.search_mode = search_mode
        # run-scoped tier: always include the `recent_first` most recent memories of
        # this task before similarity hits — in a derivation chain the previous lemma is
        # the dependency the question never names (76/80 questions carry no explicit
        # ref)
        self.recent_first = recent_first
        self.min_body_chars = min_body_chars
        self.namespace_scope = namespace_scope
        self.shared_namespace = shared_namespace
        self.name_tags = name_tags
        self.write_entities = write_entities
        self.actor_ids = actor_ids
        self.source_span_chars = source_span_chars
        self.anchor_first = anchor_first
        self.run_id = ""
        # results ledger (MemoryArena read, data point 1, client-side emulation
        # of run.checkpoint): every subtask's statement → claim, compact, in a
        # sibling namespace, ALL of them listed before the similarity hits
        self.ledger = ledger
        self.ledger_claim_chars = ledger_claim_chars
        self.base_url = os.environ.get("LOOM_BASE_URL", "http://127.0.0.1:7777").rstrip("/")
        self.token = os.environ["LOOM_API_KEY"]
        self.org = os.environ.get("LOOM_ORG", "dev")
        # hash the whole gym user_id: the travel runner's ids share their first
        # 8 chars across arms ("data_1_gpt-5-mini_<arm>"), which merged namespaces
        _h = hashlib.sha1(self.user_id.encode()).hexdigest()[:10]
        self.namespace = f"bench-ma-{mode}-{_h}"
        if shared_namespace:
            # the production shape: one store, many tasks; run_id is the task scope
            self.namespace = shared_namespace
        # every episode is a loom run; run.complete comes from the
        # runner's /memory/finish with the episode's pass/fail
        self.run_id = f"r{_h}"
        self.used_keys: list[str] = []
        self.agent = "memoryarena"
        self.tokenizer = tiktoken.encoding_for_model("gpt-4o-mini")
        self.n = 0
        self.session = requests.Session()
        self.latency_log = os.environ.get("LOOM_MA_LATENCY_LOG", "")

        # -- wire ---------------------------------------------------------------
        try:
            self._post(
                "run.begin",
                {
                    **self._identity(),
                    "run_id": self.run_id,
                    "goal": f"MemoryArena episode {self.user_id}",
                    "environment": "memoryarena",
                    "consumer": "bench",
                },
            )
        except Exception as e:  # noqa: BLE001 — the ledger never blocks the gym
            print(f"[loom plugin] run.begin failed: {e!r}", file=sys.stderr, flush=True)

    def _post(self, op: str, body: dict) -> dict:
        """One loom call. A server restart mid-run must not lose a subtask's memory, so
        connection errors and 5xx retry with backoff for up to ~2 minutes; 4xx raise at
        once (a contract error, not an outage)."""
        last: Exception | None = None
        for attempt in range(8):
            t0 = time.time()
            try:
                r = self.session.post(
                    f"{self.base_url}/v1/{op}",
                    json=body,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=120,
                )
            except requests.RequestException as e:  # connection refused / reset
                last = e
                time.sleep(min(30, 2**attempt))
                continue
            dt = time.time() - t0
            if self.latency_log:
                with open(self.latency_log, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "ns": self.namespace,
                                "op": op,
                                "ms": int(dt * 1000),
                                "status": r.status_code,
                                "attempt": attempt,
                            }
                        )
                        + "\n"
                    )
            if r.status_code >= 500:
                last = requests.HTTPError(f"{op}: {r.status_code}")
                time.sleep(min(30, 2**attempt))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"loom {op} failed after retries: {last}")

    def _identity(self) -> dict:
        return {"org": self.org, "namespace": self.namespace, "agent": self.agent, "user_id": "-"}

    def _split(self, chunk: str) -> list[str]:
        toks = self.tokenizer.encode(chunk, disallowed_special=())
        if len(toks) <= self.max_tokens:
            return [chunk]
        return [
            self.tokenizer.decode(toks[i : i + self.max_tokens])
            for i in range(0, len(toks), self.max_tokens)
        ]

    # -- gym contract -------------------------------------------------------
    @staticmethod
    def _claim(chunk: str, limit: int) -> tuple[str, str]:
        """(task head, claim) from the gym's '## Task: … ## solution: …' entry:
        the claim is the solution's first paragraph — the result a later step
        reuses — never the proof body."""
        t = chunk
        if t.startswith('"'):
            try:
                t = json.loads(t)
            except Exception:  # noqa: BLE001
                t = t.strip('"')
        task = t.split("## Task:", 1)[1] if "## Task:" in t else t
        task, _, sol = task.partition("## solution:")
        task = re.sub(r"\s+", " ", task).strip()[:240]
        para = re.split(r"\n\s*\n", sol.strip(), maxsplit=1)[0] if sol.strip() else ""
        return task, re.sub(r"\s+", " ", para).strip()[:limit]

    @staticmethod
    def _traveler_tags(piece: str) -> list[str]:
        """The gym's travel chunk is JSON with a top-level "name"; tag it so a
        later joiner who cites that traveler can retrieve by name."""
        m = re.search(r'"name":\s*"([^"]+)"', piece)
        return [f"traveler:{m.group(1).strip().lower()}"] if m else []

    @staticmethod
    def _actor(piece: str) -> str:
        """The traveler a gym chunk belongs to, as a loom user_id ("-" when the
        chunk names nobody)."""
        m = re.search(r'"name":\s*"([^"]+)"', piece)
        return m.group(1).strip().lower() if m else "-"

    @staticmethod
    def _cited_names(prompt: str) -> list[str]:
        """Travelers a query refers to: "X's", "join X", "same … as X", and every
        name in the "with / joining A, B, and C" clause; never the speaker."""
        me = re.search(r"\bI am ([A-Z][a-z]+)", prompt)
        names = set(re.findall(r"\b([A-Z][a-z]+)'s\b", prompt))
        names |= set(re.findall(r"\bjoin ([A-Z][a-z]+)", prompt))
        names |= set(re.findall(r"\b(?:as|than) ([A-Z][a-z]+)\b", prompt))
        for clause in re.findall(
            r"\b(?:with|joining) ([A-Z][a-z]+(?:,? (?:and )?[A-Z][a-z]+)*)", prompt
        ):
            names |= set(re.findall(r"[A-Z][a-z]+", clause))
        names -= {"I", "Day", "For", "On"}
        if me:
            names.discard(me.group(1))
        return sorted(names)

    def add_chunk(self, chunk: str):
        if self.ledger and chunk.strip():
            task, claim = self._claim(chunk, self.ledger_claim_chars)
            if claim:
                self._post(
                    "memory.set",
                    {
                        **self._identity(),
                        "namespace": f"{self.namespace}-ledger",
                        "memory_key": f"ledger_{self.n + 1:04d}",
                        "memory_type": "semantic",
                        "value": f"[step {self.n + 1}] {task} → RESULT: {claim}",
                        "tags": ["memoryarena", "ledger"],
                    },
                )
        for piece in self._split(chunk):
            if not piece.strip():
                continue
            self.n += 1
            writer = {
                **self._identity(),
                **({"user_id": self._actor(piece)} if self.actor_ids else {}),
            }
            if self.mode == "verbatim":
                self._post(
                    "memory.set",
                    {
                        **writer,
                        "memory_key": (f"{self.run_id}/" if self.run_id else "")
                        + f"session_{self.n:04d}",
                        **({"run_id": self.run_id} if self.run_id else {}),
                        "memory_type": "semantic",
                        "value": piece,
                        "tags": ["memoryarena", f"session:{self.n}"] + self._traveler_tags(piece),
                        **(
                            {"entities": [t.split(":", 1)[1] for t in self._traveler_tags(piece)]}
                            if self.write_entities
                            else {}
                        ),
                    },
                )
            else:
                self._post(
                    "memory.set_from_messages",
                    {
                        **writer,
                        "messages": [{"role": "user", "content": piece}],
                        "max_tokens": 4096,
                        **({"run_id": self.run_id} if self.run_id else {}),
                    },
                )
        return {"stored": self.n}

    def finish(self, status: str, metadata: dict | None = None) -> dict:
        """run.complete for the episode: completed|failed, the keys its searches
        returned as memories_used, per-round rewards in metadata."""
        try:
            return self._post(
                "run.complete",
                {
                    "org": self.org,
                    "namespace": self.namespace,
                    "run_id": self.run_id,
                    "status": "completed" if status == "completed" else "failed",
                    "outcome": "positive" if status == "completed" else "negative",
                    "memories_used": list(dict.fromkeys(self.used_keys))[:500],
                    "metadata": {"environment": "memoryarena", **(metadata or {})},
                },
            )
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def wrap_user_prompt(self, prompt: str) -> str:
        lines = ["<memory_context>"]
        hits = []
        if self.n:
            try:
                # bodies="unit": whole stored lemmas by rank until the budget — a
                # 1,200-char clip loses the derivation
                res = self._post(
                    "memory.search",
                    {
                        "org": self.org,
                        "namespace": self.namespace,
                        "agent": "*",
                        "query": prompt[:4000],
                        "top_k": (len(self._cited_names(prompt)) + 1)
                        if (self.name_tags and self._cited_names(prompt))
                        else self.top_k,
                        **(
                            {"tags": [f"traveler:{n.lower()}" for n in self._cited_names(prompt)]}
                            if (self.name_tags and self._cited_names(prompt))
                            else {}
                        ),
                        "bodies": self.bodies,
                        "body_budget_tokens": min(
                            32000, self.body_budget_tokens * (3 if self.name_tags else 1)
                        ),
                        # pinned, so a change of the server's default mode does
                        # not move the arm
                        "search_mode": self.search_mode,
                        **({"recent_first": self.recent_first} if self.recent_first else {}),
                        **({"min_body_chars": self.min_body_chars} if self.min_body_chars else {}),
                        **(
                            {"namespace_scope": self.namespace_scope}
                            if self.namespace_scope
                            else {}
                        ),
                        **({"run_id": self.run_id} if self.shared_namespace else {}),
                        **({"anchor_first": self.anchor_first} if self.anchor_first else {}),
                        **(
                            {"source_span_chars": self.source_span_chars}
                            if self.source_span_chars
                            else {}
                        ),
                    },
                )
                hits = res.get("results") or res.get("hits") or []
                if self.name_tags and not hits and self._cited_names(prompt):
                    # a cited name with no stored trace: fall back to similarity
                    res = self._post(
                        "memory.search",
                        {
                            "org": self.org,
                            "namespace": self.namespace,
                            "agent": "*",
                            "query": prompt[:4000],
                            "top_k": self.top_k,
                            "bodies": self.bodies,
                            "body_budget_tokens": self.body_budget_tokens,
                            "search_mode": self.search_mode,
                            **(
                                {"namespace_scope": self.namespace_scope}
                                if self.namespace_scope
                                else {}
                            ),
                            **({"run_id": self.run_id} if self.shared_namespace else {}),
                        },
                    )
                    hits = res.get("results") or res.get("hits") or []
                self.used_keys += [str(h.get("memory_key")) for h in hits if h.get("memory_key")]
                if self.name_tags and self.n:
                    # the base traveler's plan carries the dates and origin: always first
                    base_key = (f"{self.run_id}/" if self.run_id else "") + "session_0001"
                    if base_key not in {h.get("memory_key") for h in hits}:
                        g = self._post("memory.get", {**self._identity(), "memory_key": base_key})
                        if g.get("found"):
                            hits = [
                                {"memory_key": base_key, "content_excerpt": g.get("value")}
                            ] + hits
            except Exception as e:  # noqa: BLE001 — the gym must keep running
                lines.append(f"None (search error: {type(e).__name__})")
        if not hits and len(lines) == 1:
            lines.append("None")
        if self.ledger and self.n:
            try:
                lst = (
                    self._post(
                        "memory.list",
                        {
                            **self._identity(),
                            "namespace": f"{self.namespace}-ledger",
                            "prefix": "ledger_",
                            "limit": 100,
                        },
                    ).get("entries")
                    or []
                )
                keys = sorted(e["memory_key"] for e in lst)
                vals = []
                for k in keys:
                    g = self._post(
                        "memory.get",
                        {
                            **self._identity(),
                            "namespace": f"{self.namespace}-ledger",
                            "memory_key": k,
                        },
                    )
                    if g.get("found"):
                        vals.append(str(g.get("value")))
                if vals:
                    lines.append(
                        "<memory>Results established so far in this task:\n"
                        + "\n".join(vals)
                        + "</memory>"
                    )
            except Exception as e:  # noqa: BLE001
                lines.append(f"<memory>ledger unavailable ({type(e).__name__})</memory>")
        for h in hits:
            text = h.get("content_excerpt") or h.get("value") or ""
            if isinstance(text, (dict, list)):
                text = json.dumps(text)
            # A row written under an actor's user_id says "User …"; the owner
            # label is what tells the planner whose value it is.
            owner = str(h.get("user_id") or "")
            label = f"[{owner}] " if owner and owner != "-" else ""
            lines.append(f"<memory>{label}{text}</memory>")
        lines.append("</memory_context>")
        lines.append(f"User: {prompt}")
        return "\n".join(lines)
