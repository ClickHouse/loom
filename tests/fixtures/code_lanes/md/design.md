# Ledger design

The record, preserved. See the [glossary](glossary.md#watermarks) and the
spec in [Ledger.tla](../tla/Ledger.tla).

## Commit

A record is durable when `durable` covers its LSN; `Advance` moves the
watermark and `Stored` decides when. The `ledger-lib` target holds the code.

### Failure

`Recover` is not modelled here.

## Glossary link

Terms live in the glossary.
