# Loom reference extractor

The service the Loom server sends an original to and gets its extraction
artifact back from. Loom parses no original in its own process; this service
does, one extraction per non-dumpable child process under memory, CPU and time
limits, and holds no Loom or database credential. Linux only.

It parses Markdown, HTML, plain text, SQL, TOML, CSV, PDF, DOCX, PPTX, XLSX
and, when pyarrow is installed, Parquet.
Another extractor (for a format this one does not parse, or a utility an
operator prefers) serves the same wire contract:
[extractor contract](../docs/reference/extractor-contract.md).
The design is in [document extraction](../docs/architecture/document-extraction.md).

Run it as a user of its own, and install it where that user cannot write, since
a file of that user's on `sys.path` would change what every later extraction
imports ([what an extraction can do](../docs/architecture/document-extraction.md#the-service-boundary)).
The token goes in a file only root reads and reaches the service through its
environment, never a command line, which any local user can read:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin loom-extractor
sudo python3 -m venv /usr/local/lib/loom-extractor && sudo /usr/local/lib/loom-extractor/bin/pip install ./extractor
sudo sh -c 'umask 077 && openssl rand -hex 24 > /etc/loom-extractor.token'
sudo sh -c 'export LOOM_EXTRACTOR_TOKEN="$(cat /etc/loom-extractor.token)" LOOM_EXTRACTOR_HOST=0.0.0.0 &&
  exec setpriv --reuid=loom-extractor --regid=loom-extractor --init-groups /usr/local/lib/loom-extractor/bin/loom-extractor'
```

| Variable                            | Default     | Meaning                                                         |
| ----------------------------------- | ----------- | --------------------------------------------------------------- |
| `LOOM_EXTRACTOR_TOKEN`              | (required)  | the bearer token every `/v1` call must carry, 16+ chars         |
| `LOOM_EXTRACTOR_HOST`               | `127.0.0.1` | the address to listen on                                        |
| `LOOM_EXTRACTOR_PORT`               | `8790`      | the port to listen on                                           |
| `LOOM_EXTRACTOR_TIMEOUT_S`          | `120`       | wall-clock and CPU seconds one extraction may take              |
| `LOOM_EXTRACTOR_MEMORY_MB`          | `1024`      | the address space one extraction's process may use              |
| `LOOM_EXTRACTOR_CONCURRENCY`        | `1`         | sources read and extracted at once, the rest waiting; see below |
| `LOOM_EXTRACTOR_MAX_BYTES`          | `67108864`  | the largest original accepted                                   |
| `LOOM_EXTRACTOR_MAX_ARTIFACT_BYTES` | `67108864`  | the largest artifact an extraction may write                    |

The image (`extractor/Dockerfile`, built from the repository root; released as
`ghcr.io/clickhouse/loom-extractor`) does this for you: the venv is root's, the
service runs as uid 10001, and Loom's compose stack and Helm chart run it with no
route out and the token alone
([deployment](../docs/operations/deployment.md#container-images)):

```bash
sudo sh -c 'umask 077 && printf "LOOM_EXTRACTOR_TOKEN=%s\n" "$(openssl rand -hex 24)" > /etc/loom-extractor.env'
sudo docker run --read-only --cap-drop ALL --security-opt no-new-privileges \
  -p 127.0.0.1:8790:8790 --env-file /etc/loom-extractor.env ghcr.io/clickhouse/loom-extractor:X.Y.Z
```

with `X.Y.Z` the server's version, as the compose stack and the chart run it,
and `/etc/loom-extractor.env` root's alone (the `docker` command reads it, hence
`sudo`), for a server on the same host at `http://127.0.0.1:8790`. Unlike the
compose stack and the chart, this leaves the service's own process a route out;
an extraction can make no socket either way (with one at a time, the default:
see below).

Point the server at it with `LOOM_DOCUMENT_EXTRACTOR_URL` and the token
(`/etc/loom-extractor.token`, or the value in `/etc/loom-extractor.env`) in
`LOOM_DOCUMENT_EXTRACTOR_TOKEN`.

Run one extraction at a time per service and scale with replicas. A child is
readable by other processes of the same user for the moment between its start
and the point it makes itself non-dumpable; with `LOOM_EXTRACTOR_CONCURRENCY`
above 1, a child that a hostile source has taken over could open a concurrent
child's source in that moment. The service refuses to start as root or with
`CAP_SYS_RESOURCE` or `CAP_SYS_ADMIN` permitted, as the kernel does not hold such
a process to a child's limit on new processes, and on an architecture other than
x86_64 and aarch64, which its socket filter does not cover. It sets
no-new-privileges, so no setuid binary or file capability gives a child one, and
each child sets a seccomp filter under which it can make no socket, so it reaches
no address, and cannot use `ptrace` or `process_vm_*` on another process. A
concurrent sibling is still writable through `/proc/<pid>/mem` until it makes
itself non-dumpable, which is one more reason to keep one extraction at a time.
