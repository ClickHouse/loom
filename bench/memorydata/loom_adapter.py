"""Loom as a MemoryData method (harness: github.com/OpenDataBox/MemoryData).

This file is the copy kept in the loom repo; drop it at methods/loom/loom_adapter.py
in a MemoryData checkout and follow bench/memorydata/README.md for the six
name-list edits the harness needs.

Contract mirrored from the harness's raw-text reference adapter
(methods/<system>/*_adapter.py): the harness calls
`add_chunk(content, timestamp)` once per context chunk while memorizing and
`retrieve(question) -> list[str]` per query; it generates the answer itself
from the returned texts and scores LoCoMo source recall by parsing
`[LOCOMO_META chunk_id=… source_ids=…]` headers out of them.

Two arms, chosen by `loom_mode`:

* `verbatim` — each chunk is stored whole with `memory.set` (the storage text,
  META header included) under `session_id = chunk_id`. Retrieval returns the
  stored value via `bodies=unit`, so the META header survives and recall is
  scored the way the harness scores its raw-text reference arm.
* `extract` — each chunk goes through `memory.set_from_messages` (loom's
  schema-free LLM extraction) with `session_id = chunk_id`; extracted facts
  do not carry the META header, so retrieve() re-attaches it from the hit's
  `session_id` through a chunk_id → source_ids map kept by
  the adapter. This is what makes loom's fact plane scoreable on their
  Recall@k axis.

One loom namespace per (dataset, context): `<prefix>-<sub_dataset>-<context>`,
created lazily by the first write. Env: LOOM_BASE_URL (default
http://127.0.0.1:7777), LOOM_API_KEY (required).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

import httpx
from utils.locomo_utils import (
    build_locomo_storage_text,
    parse_locomo_metadata,
    parse_locomo_source_ids,
    strip_locomo_metadata,
)

_SESSION_HEADER_RE = re.compile(r"^Session (\d+)(?: \((.*?)\))?\s*$")
_TURN_RE = re.compile(r"^([^:\n]{1,80}):\s(.*)$")


class LoomAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        namespace: str,
        retrieve_num: int,
        mode: str = "verbatim",
        search_mode: str = "semantic",
        bodies: str = "unit",
        body_budget_tokens: int = 6000,
        request_timeout_s: float = 120.0,
        state_path: str | None = None,
        org: str | None = None,
    ) -> None:
        self.org = org or os.environ.get("LOOM_ORG") or "dev"
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.namespace = namespace
        self.retrieve_num = int(retrieve_num)
        self.mode = mode
        self.search_mode = search_mode
        self.bodies = bodies
        self.body_budget_tokens = int(body_budget_tokens)
        self.state_path = state_path
        self._client = httpx.Client(timeout=request_timeout_s, trust_env=False)
        self._n = 0
        # chunk_id -> source_ids, so extracted facts can be scored for recall
        self._sources: dict[str, list[str]] = {}
        self.write_seconds = 0.0
        self.write_tokens_in = 0
        self.write_tokens_out = 0
        self.write_calls = 0
        self.write_errors = 0
        if state_path and os.path.exists(state_path):
            try:
                saved = json.loads(open(state_path, encoding="utf-8").read())
                self._n = int(saved.get("n", 0))
                self._sources = dict(saved.get("sources", {}))
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ http
    def _identity(self) -> dict[str, str]:
        return {"org": self.org, "namespace": self.namespace, "agent": "memorydata", "user_id": "-"}

    def _post(self, op: str, body: dict[str, Any], attempts: int = 4) -> dict[str, Any]:
        url = f"{self.base_url}/v1/{op}"
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                r = self._client.post(
                    url, json=body, headers={"Authorization": f"Bearer {self.token}"}
                )
                if r.status_code == 429 or r.status_code >= 500:
                    delay = float(r.headers.get("Retry-After") or (1.5 * (attempt + 1)))
                    time.sleep(min(delay, 20.0))
                    last = RuntimeError(f"{op}: HTTP {r.status_code}")
                    continue
                if r.status_code >= 400:
                    # 4xx is a contract error, not a transient: surface the body once.
                    raise RuntimeError(f"{op}: HTTP {r.status_code} {r.text[:400]}")
                return r.json()
            except httpx.HTTPError as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{op} failed after {attempts} attempts: {last}")

    # --------------------------------------------------------------- parsing
    @staticmethod
    def _parse_turns(text: str, fallback_timestamp: str | None) -> tuple[list[dict[str, str]], str]:
        """Split a LoCoMo chunk ("Session N (date)" header, "Speaker: text"
        lines) into loom messages. Returns (messages, observation_date)."""
        messages: list[dict[str, str]] = []
        date = ""
        for raw in text.split("\n"):
            line = raw.strip()
            if not line:
                continue
            m = _SESSION_HEADER_RE.match(line)
            if m:
                date = (m.group(2) or "").strip() or date
                continue
            t = _TURN_RE.match(line)
            if t:
                speaker, body = t.group(1).strip(), t.group(2).strip()
                messages.append({"role": "user", "content": f"{speaker}: {body}", "name": speaker})
            elif messages:
                messages[-1]["content"] += "\n" + line
            else:
                messages.append({"role": "user", "content": line})
        return messages, (date or (fallback_timestamp or ""))

    # ---------------------------------------------------------------- write
    def add_chunk(self, content: str, timestamp: str | None = None) -> None:
        raw = str(content or "")
        metas = parse_locomo_metadata(raw)
        chunk_id = str(metas[0].get("chunk_id") if metas else "") or f"chunk_{self._n + 1:05d}"
        source_ids = (
            list(metas[0].get("source_ids") or []) if metas else parse_locomo_source_ids(raw)
        )
        self._sources[chunk_id] = source_ids
        self._n += 1
        t0 = time.perf_counter()
        try:
            if self.mode == "extract":
                messages, obs_date = self._parse_turns(strip_locomo_metadata(raw), timestamp)
                body: dict[str, Any] = {
                    **self._identity(),
                    "session_id": chunk_id,
                    "messages": messages
                    or [{"role": "user", "content": strip_locomo_metadata(raw)}],
                }
                if obs_date:
                    body["observation_date"] = obs_date
                resp = self._post("memory.set_from_messages", body)
                self.write_tokens_in += int(resp.get("tokens_in") or 0)
                self.write_tokens_out += int(resp.get("tokens_out") or 0)
            else:
                # Whole chunk, META header kept, one row keyed by the chunk id.
                # No per-chunk session_id here: point reads (memory.get and the
                # bodies=unit expansion) resolve a key under the READ identity's
                # session, so a row written under session=chunk_id would come
                # back as the 1,200-char clip. The META header in the value
                # already names the sources; the extract arm is the one that
                # needs session_id for provenance.
                self._post(
                    "memory.set",
                    {
                        **self._identity(),
                        "memory_key": chunk_id,
                        "memory_type": "semantic",
                        "value": raw,
                        "tags": ["memorydata", "locomo"],
                    },
                )
            self.write_calls += 1
        except Exception:
            self.write_errors += 1
            raise
        finally:
            self.write_seconds += time.perf_counter() - t0
            self._save_state()

    # ----------------------------------------------------------------- read
    def retrieve(self, question: str) -> list[str]:
        resp = self._post(
            "memory.search",
            {
                **self._identity(),
                "query": question,
                "top_k": self.retrieve_num,
                "search_mode": self.search_mode,
                "namespace_scope": "this",
                "auto_touch": False,
                "bodies": self.bodies,
                "body_budget_tokens": self.body_budget_tokens,
            },
        )
        out: list[str] = []
        for h in resp.get("results") or []:
            text = h.get("value") if isinstance(h.get("value"), str) else h.get("content_excerpt")
            text = str(text or "").strip()
            if text.startswith('"'):
                # loom stores a string value JSON-encoded; the excerpt / unit
                # body comes back in that form. Decode so the META header and
                # the newlines are the ones the harness wrote.
                try:
                    text = str(json.loads(text))
                except ValueError:
                    text = text.strip('"').replace("\\n", "\n")
            if not text:
                continue
            if not parse_locomo_source_ids(text):
                # Extracted fact: recover the source ids from the session it
                # was written under so recall can be scored.
                sid = str(h.get("session_id") or "")
                srcs = self._sources.get(sid)
                if srcs:
                    text = build_locomo_storage_text(text, sid, srcs)
            prefix = str(h.get("valid_at") or "").strip()
            out.append(
                f"{prefix} {text}".strip()
                if prefix and not text.startswith("[LOCOMO_META")
                else text
            )
        return out

    def ask(self, question: str) -> str:  # the harness answers from retrieve(); kept for parity
        return "\n".join(self.retrieve(question))

    def finalize(self) -> None:
        self._save_state()

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            # A bare filename has no directory part, and makedirs("") raises.
            if os.path.dirname(self.state_path):
                os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"n": self._n, "sources": self._sources, "namespace": self.namespace}, f)
        except Exception as e:  # noqa: BLE001 — the run continues; a resume will re-ingest
            print(f"[loom adapter] state not saved to {self.state_path}: {e}", file=sys.stderr)
