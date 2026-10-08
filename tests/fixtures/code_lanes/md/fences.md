# Fenced examples

Prose names the spec's operators: `Init` is a reference, and `Append` names
both a TLA+ operator and a FlatBuffers RPC, so it binds neither. See the
[watermarks](glossary.md#watermarks) entry.

```python
def Advance():
    return Store(1)
```

```tla
Next == Append(1)
```

```sh
# a backticked name inside a fence is code, not a reference
echo `Advance` `Next`
```
