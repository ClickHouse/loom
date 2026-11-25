#!/bin/bash
set -euo pipefail

# Only this unmodified standard-library module and the maintained oracle are packaged.
python3 -m tests.fuzz.embedding_identity_fuzzer --replay tests/fuzz/corpus/embedding_identity
pyinstaller --distpath "$OUT" --onefile --paths /src/loom \
  --hidden-import server.core.embedding_space \
  --hidden-import tests.fuzz.embedding_identity_oracle \
  --name embedding_identity.pkg tests/fuzz/embedding_identity_fuzzer.py
cat >"$OUT/embedding_identity" <<'WRAPPER'
#!/bin/sh
# LLVMFuzzerTestOneInput
this_dir=$(dirname "$0")
exec "$this_dir/embedding_identity.pkg" "$@"
WRAPPER
chmod +x "$OUT/embedding_identity"
zip -j "$OUT/embedding_identity_seed_corpus.zip" tests/fuzz/corpus/embedding_identity/*
printf '[libfuzzer]\nmax_len = 4096\ntimeout = 5\nrss_limit_mb = 512\n' >"$OUT/embedding_identity.options"
