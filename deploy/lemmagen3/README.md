# Lemmagen3 English model

`en.bin` is the English lemmatization model ClickHouse loads for
`lemmatize('en', ...)` (registered in `deploy/clickhouse-config/lemmatizers.xml`).

## Provenance

|                  |                                                                                                                                                                                                                                                                                                                                                   |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Upstream         | [lemmagen3](https://github.com/vpodpecan/lemmagen3) (PyPI `lemmagen3`), file `lemmagen3/models/en.bin`                                                                                                                                                                                                                                            |
| Verified against | `lemmagen3==3.5.2`: byte-identical (`cmp`) to the wheel's `models/en.bin`                                                                                                                                                                                                                                                                         |
| sha256           | `7320a49f7e6eb02a38c8c3d820c63b7e94a3a69449dfe73e920c9da47891910d`                                                                                                                                                                                                                                                                                |
| Size             | 53,860 bytes                                                                                                                                                                                                                                                                                                                                      |
| License          | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), per the upstream LICENSE: the English model is built from the Multext / Multext-East language resources (<http://nl.ijs.si/ME/V4/>). The lemmatizer code upstream is LGPL (C++ from Lemmagen, <http://lemmatise.ijs.si/>) and MIT (the Python module); neither is vendored here. |

Attribution: the lemmagen3 project (see its LICENSE for the authors), built
from the Multext-East resources of the Jožef Stefan Institute. The file is
unmodified.

## Consumers

- `deploy/clickhouse-config/lemmatizers.xml` points ClickHouse at it; the dev
  compose stack, `dev/ci_start_clickhouse.sh` and the analyzer corpus
  instructions (`tests/analyzer/README.md`) mount this directory at
  `/var/lib/clickhouse/lemmagen3`.
- SQL: the migration runner's NLP probe (`SELECT lemmatize('en', 'tests')` in
  `server/core/schema/migrate.py`). No schema expression calls `lemmatize`;
  `content_excerpt_lemma` is filled by the server.
- Python: `server/memory/domain/lemmatize.py` uses the `lemmagen3` package's own
  copy of this model, so the two sides agree only while the package ships
  this file. After upgrading `lemmagen3`, compare checksums:

  ```bash
  python -c "import lemmagen3, pathlib; print(pathlib.Path(lemmagen3.__file__).parent / 'models/en.bin')" \
    | xargs sha256sum
  sha256sum deploy/lemmagen3/en.bin
  ```

ClickHouse Cloud does not read this file; there the server-side lemma column
is written by Python alone.
