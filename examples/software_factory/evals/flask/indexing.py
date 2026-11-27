"""Pinned Python compiler proof for the Flask evaluation."""

from __future__ import annotations

import time

from examples.software_factory.factory.model import digest


class ScopeVerifier:
    """Verify every lookup against an externally approved immutable index receipt.

    A matching watermark alone does not prove immutability or reader permission.
    The protocol must also pin an audited namespace/authorization certificate.
    Restricted counts are never interpreted as an empty index.
    """

    def __init__(
        self,
        scope: dict,
        *,
        index_repository: str,
        build_commit: str,
        certificate_sha256: str,
        index_tier: str,
        compiler_rows: dict,
    ):
        if not all(
            len(v) == n and all(c in "0123456789abcdef" for c in v)
            for v, n in (
                (scope.get("commit", ""), 40),
                (build_commit, 40),
                (certificate_sha256, 64),
            )
        ):
            raise ValueError("Freeze full source, server build and certificate hashes")
        if not index_repository or set(scope) != {"org", "namespace", "repository", "commit"}:
            raise ValueError("Complete explicit index scope is required")
        self.scope, self.repository, self.build = dict(scope), index_repository, build_commit
        self.certificate = certificate_sha256
        self.index_tier = index_tier
        keys = {
            "compiler_definitions",
            "compiler_references",
            "python_definitions",
            "python_references",
        }
        if set(compiler_rows) != keys or any(
            type(n) is not int or n < 0 for n in compiler_rows.values()
        ):
            raise PermissionError("Frozen compiler row proof is incomplete")
        if any(
            compiler_rows["compiler_" + kind] < compiler_rows["python_" + kind]
            for kind in ("definitions", "references")
        ):
            raise PermissionError("Python compiler rows exceed the recorded totals")
        self.compiler_rows = dict(compiler_rows)
        if index_tier == "treesitter":
            if any(compiler_rows.values()):
                raise PermissionError("Tree-sitter scope contains compiler rows")
        elif index_tier == "scip":
            if any(compiler_rows["python_" + kind] <= 0 for kind in ("definitions", "references")):
                raise PermissionError(
                    "SCIP requires real Python compiler definitions and references"
                )
        else:
            raise ValueError("Select an explicit treesitter or scip index tier")

    async def __call__(self, agent, body: dict) -> dict:
        if any(body.get(k) != self.scope[k] for k in ("org", "namespace")):
            raise PermissionError("Lookup scope differs from the frozen protocol")
        started = time.monotonic()
        result = await agent.call_with_meta(
            "code.index_status",
            {
                "org": self.scope["org"],
                "namespace": self.scope["namespace"],
                "brief": True,
            },
            retry={"max_attempts": 1},
        )
        data = result.data
        expected = {
            "org": self.scope["org"],
            "namespace": self.scope["namespace"],
            "repo": self.repository,
            "git_head": self.scope["commit"],
            "build_commit": self.build,
        }
        if not isinstance(data, dict) or any(data.get(k) != v for k, v in expected.items()):
            raise PermissionError("Index identity, watermark or build is unverified")
        if data.get("ignored_args") or data.get("restricted") or data.get("abstained"):
            raise PermissionError("Index scope was restricted or ignored")
        tiers = data.get("compiler_tiers", [])
        if not isinstance(tiers, list) or any(not isinstance(t, dict) for t in tiers):
            raise PermissionError("Compiler tier status is malformed")
        if self.index_tier == "treesitter":
            if any(v for k, v in data.items() if k.startswith("scip_commit")) or any(
                t.get("commit") for t in tiers
            ):
                raise PermissionError("Tree-sitter scope has a compiler watermark")
        elif data.get("scip_commit_py") != self.scope["commit"] or not any(
            all(
                t.get(k) == v
                for k, v in {
                    "producer": "python",
                    "tool": "scip-python",
                    "watermark_key": "scip_commit_py",
                    "commit": self.scope["commit"],
                }.items()
            )
            for t in tiers
        ):
            raise PermissionError("Pinned Python SCIP watermark and compiler tier are unverified")
        return {
            "scope": dict(self.scope),
            "index_tier": self.index_tier,
            "compiler_rows": dict(self.compiler_rows),
            "compiler_rows_basis": "Frozen certificate with before/after trial source-index audits",
            "permission": "allowed",
            "request_id": result.request_id,
            "duration_s": time.monotonic() - started,
            "response_sha256": digest(data),
            "certificate_sha256": self.certificate,
            "cost_usd": None,
            "cost_status": "unknown",
        }
