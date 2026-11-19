"""Coverage-guided fuzzing of the code tokenizer with Atheris.

Runs `tests/unit/tokenize_oracle.check` on fuzzer-generated text. Local only: no
workflow runs it. Atheris is not a project dependency; install it in the Python 3.12
environment first (it needs a clang with libFuzzer on Linux):

    .venv/bin/pip install atheris==3.0.0
    .venv/bin/python scripts/fuzz/tokenize_atheris.py -runs=200000 -max_len=4096

Every libFuzzer flag passes through (`-max_total_time=`, a corpus directory, a crash
file to replay). Flags of this script:

    --strict-cap      also check `len(tokens) <= _MAX_TOKENS`. The known overflow needs
                      ~512 identifiers in one input, which a 5-minute run did not reach;
                      tests/unit/test_code_tokenize_oracle.py pins it with a built input
    --broken-oracle   check a tokenizer that drops its last token from `lexemes_text`,
                      to show the harness reports a failing property

A failure prints the property and the input; libFuzzer writes the input to
`crash-<sha1>`. Turn each confirmed one into a deterministic test next to the oracle.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import atheris

with atheris.instrument_imports():
    from server.code.domain.extract import tokenize
    from tests.unit.tokenize_oracle import INPUT_CEILING, check

STRICT_CAP = "--strict-cap" in sys.argv
BROKEN = "--broken-oracle" in sys.argv


def _broken_lexemes(text: str) -> str:
    return " ".join(tokenize.code_tokens(text)[:-1])


def test_one_input(data: bytes) -> None:
    text = atheris.FuzzedDataProvider(data).ConsumeUnicodeNoSurrogates(INPUT_CEILING)
    if BROKEN:
        check(text, lexemes_of=_broken_lexemes)
    else:
        check(text, strict_cap=STRICT_CAP)


def main() -> None:
    args = [a for a in sys.argv if a not in ("--strict-cap", "--broken-oracle")]
    atheris.Setup(args, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
