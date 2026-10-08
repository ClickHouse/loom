# Vendored SCIP schema

| File          | What it is                                     | sha256                                                             |
| ------------- | ---------------------------------------------- | ------------------------------------------------------------------ |
| `scip.proto`  | upstream schema, unmodified                    | `b38021b65ef90cbbf6af9c829ff75192859ad9b5da05439ef154bea4ceb2bf03` |
| `scip_pb2.py` | `protoc --python_out` output for `scip.proto`  | `70c8e8d5884ea4fb29993bced28c1922cd780ad617793981366cc2ce9f0250a9` |

- Upstream: https://github.com/scip-code/scip (formerly `sourcegraph/scip`),
  tag `v0.10.0`, commit `1c2b6db7e560d5233c944f36e4ac1377cc6963fc`, file
  `scip.proto`.
- License: Apache-2.0 (upstream `LICENSE`), the same as this repository.
- Local modifications: none to either file.
- Generator: protoc 35.1 (the release archive, sha256-pinned per platform in
  `regenerate.sh`). Its Python gencode is "Protobuf Python Version: 7.35.1",
  so the protobuf runtime must be 7.x at 7.35.1 or later (the `scip` extra in
  `pyproject.toml`).

Never edit `scip_pb2.py` by hand, and keep formatters and linters off this
directory (`pyproject.toml` and `.prettierignore` exclude it). To update:

```bash
curl -sSfL -o scripts/scip_harness/vendor/scip.proto \
  https://raw.githubusercontent.com/scip-code/scip/<tag>/scip.proto
scripts/scip_harness/vendor/regenerate.sh          # writes scip_pb2.py
scripts/scip_harness/vendor/regenerate.sh --check  # exit 1 when stale
```

then update the tag, commit and both checksums above;
`tests/unit/test_scip_vendor.py` compares them with the files.
