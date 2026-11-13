# Code intelligence for C++, Python, TypeScript, Go, Rust, Java and infrastructure code

Loom's code index answers a coding agent's structure questions in the languages a
repository holds: where a symbol is defined, who calls it, and what a change touches.
Needs a repository added and `LOOM_CODE_INGEST_ENABLED=true`. Every language gets the fast
tier, parsed with tree-sitter grammars or hand-written scanners; C/C++, Python, TypeScript
and Go can add a compiler tier that resolves names the way the compiler does. The full
list of extensions and edges is the
[language support table](../features/code-index/overview.md#language-support), and the
compiler tiers' setup is in
[code index operations](../operations/code-index-operations.md#compiler-tier).

An answer names the tier behind it (`tier: "scip"` for the compiler tier, `"treesitter"`
for the fast tier), and `code.coverage` reports what a namespace holds per language and
tier. Read it before trusting `code.callers` or `code.impact` answers in a language you
have not used Loom with.

## C and C++

Symbols, calls, includes, inheritance and overrides come from the fast tier; the
compiler tier (`scip-clang`) resolves overloads, templates and namespaces from the
project's build, so `code.symbol` with `uses_brief: true` on a qualified name such as
`DB::IMergeTreeDataPart::getBytesOnDisk` returns the one definition, its call-site count
and its top call sites with their enclosing functions, each marked `tier: "scip"`. The
compiler tier needs the project's CMake build setup.

## Python

Calls, imports, inheritance and overrides come from the fast tier, notebooks' code cells
included; the compiler tier (`scip-python`) resolves names across modules. In Loom's own
repository, `code.symbol` on `writer_actor` returns its definition in
`server/memory/app/provenance.py` with its signature and docstring at the compiler tier,
and `uses_brief: true` adds its call sites.

## TypeScript and JavaScript

Calls (including `new`), imports, inheritance and overrides come from the fast tier, as
do the script blocks of Vue, Svelte and Astro components; an Angular `.component.html`
template is indexed as one unit with its component. The compiler tier (`scip-typescript`)
resolves names through the project's `tsconfig.json`, `jsconfig.json` or `package.json`.

## Go

Calls, interface embedding and structural implementation come from the fast tier, and
SQL in Go strings becomes table references; the compiler tier (`scip-go`) resolves names
across packages, monorepos included.

## Rust and Java

Rust gets calls, `use` imports and trait implementations; Java gets calls, with imports
binding names, and SQL in Java strings. Both have the fast tier only: a call bound in the
same file has confidence 1.0, through the enclosing class 0.8 and by a repository-wide name
match 0.6, a call it cannot bind is declined rather than guessed, and `code.impact` adds
mapped tests and co-change partners at file level.

## SQL and migrations

`.sql` files are indexed for search and grep by default. With
`LOOM_CODE_SQL_STATEMENT_RECORDS` on, each statement becomes a record with its table
references, so an agent asks which migrations and queries read or write a table. Turning
it on for a namespace already indexed needs a `loom code backfill --paths-file` of the SQL
files ([switches](../features/code-index/overview.md#language-support)).

## Infrastructure as code

Terraform and HCL, Kubernetes manifests, Helm charts and Go templates, Jsonnet, CMake,
Protocol Buffers and shell scripts are indexed with their references, includes and calls,
so an agent follows a Helm `include` to its `define`, or a Terraform reference to what it
names. Dockerfile, Makefile, Jenkinsfile and CI YAML jobs become symbols with
`LOOM_CODE_HOST_ISLANDS` on, after a backfill of those files when the namespace is already
indexed.

## Start

1. [Index a repository](../features/code-index/repositories.md). Which compiler tiers run
   for it is in [code index operations](../operations/code-index-operations.md#compiler-tier).
2. [Connect a coding agent](../getting-started/connect-a-coding-agent.md) on the `coding`
   toolset.
3. Ask a structure question; check `code.coverage` for the languages you rely on.

## Related

- [A code index for coding agents](code-intelligence.md)
- [Code operations](../features/code-index/operations.md)
- [Adding language support](../development/adding-language-support.md)
