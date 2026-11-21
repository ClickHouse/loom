"""Source declarations distinguish protected spellings from ordinary descriptions."""

import pytest

from scripts.i18n import translate as t
from scripts.i18n.context import Label, SourceContext, project, protected


@pytest.mark.parametrize(
    ("source_path", "label", "expected"),
    [
        ("docs/features/integrations/frameworks.md", "n8n", ("n8n",)),
        ("docs/features/integrations/index.md", "Agent Substrate", ("Agent Substrate",)),
        (
            "docs/features/integrations/index.md",
            "LibreChat v0.8.8",
            ("LibreChat v0.8.8", "LibreChat"),
        ),
        ("docs/benchmarks/results.md", "MemoryAgentBench samples", ("MemoryAgentBench",)),
        ("docs/reference/mcp.md", "OAuth", ("OAuth",)),
        ("docs/other.md", "Agent Substrate", ()),
        ("docs/other.md", "Getting started", ()),
    ],
)
def test_names_are_source_scoped_spans(source_path, label, expected):
    assert protected(Label(label), source_path) == expected


def test_a_product_name_does_not_hide_its_ordinary_descriptor():
    node = Label("MemoryAgentBench samples", minimum=2)
    atoms = protected(node, "docs/benchmarks/results.md")
    residual, spelling = project(node, atoms)
    assert residual.strip() == "samples"
    assert spelling == ("MemoryAgentBench",)
    assert project(Label("MemoryAgentBench サンプル"), atoms)[0].strip() == "サンプル"


@pytest.mark.parametrize("name", ["README", "README.md", "claude-code.md"])
def test_filename_identity_comes_from_the_source_link_target(name):
    target = "README.md" if name == "README" else name
    node = Label(name, links=((name, target),))
    files = frozenset({"docs/" + target})
    assert protected(node, "docs/other.md", files=files) == (name,)
    assert protected(node, "docs/other.md") == ()
    assert protected(Label(name, links=((name, "other.md"),)), "docs/other.md", files=files) == ()


def test_quoted_prose_is_not_a_returned_string_declaration():
    quoted = '"Install the package" / "Getting started"'
    assert protected(Label(quoted), "docs/other.md") == ()
    assert (
        protected(
            Label(quoted, field="example", string_field=True), "docs/reference/operations/a.md"
        )
        == ()
    )


def test_returned_literal_alternatives_require_a_string_field_context():
    quoted = '"namespace" / "cluster" / "scalar"'
    path = "docs/reference/operations/calibration.md"
    assert protected(Label(quoted), path) == ()
    atoms = protected(Label(quoted, field="source", string_field=True), path)
    assert project(Label(quoted), atoms)[1] == ("namespace", "cluster", "scalar")
    assert project(Label("命名空间级 / 集群级 / 标量"), atoms)[1] == ()


def test_only_the_attested_field_formula_is_protected():
    node = Label("len(content) / char_limit.", field="fill_ratio")
    assert protected(node, "docs/reference/operations/working_memory.md") == (
        "len(content) / char_limit",
    )
    assert protected(node, "docs/other.md") == ()
    assert (
        protected(
            Label("len(content) / char_limit.", field="example"),
            "docs/reference/operations/working_memory.md",
        )
        == ()
    )


def _table(rows: str) -> str:
    return "| Field | Type | Meaning |\n| --- | --- | --- |\n" + rows + "\n"


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("docs/features/integrations/frameworks.md", "n8n"),
        ("docs/features/integrations/index.md", "Agent Substrate"),
        ("docs/features/integrations/index.md", "LibreChat v0.8.8"),
        ("docs/benchmarks/results.md", "MemBench"),
        ("docs/benchmarks/results.md", "MemoryAgentBench"),
        ("docs/benchmarks/results.md", "MuSiQue-Ans"),
        ("docs/benchmarks/results.md", "PerLTQA"),
        ("docs/development/documentation-style.md", "ClickHouse Cloud / ClickHouse OSS"),
        ("docs/reference/mcp.md", "OAuth"),
    ],
)
def test_exact_source_names_pass_but_changed_or_relocated_names_fail(path, name):
    source = f"## {name}\n\n## Details\n"
    assert t.differences(source, f"## {name}\n\n## 詳細\n", source_path=path) == []
    assert t.differences(source, "## 名前\n\n## 詳細\n", source_path=path)
    assert t.differences(source, f"## 名前\n\n## {name}\n", source_path=path)
    assert t.differences(
        source, f'## 名前 <span title="{name}"></span>\n\n## 詳細\n', source_path=path
    )


@pytest.mark.parametrize("name", ["README", "README.md", "claude-code.md"])
def test_source_filename_links_pass_and_cannot_be_translated(name):
    target = "README.md" if name == "README" else name
    target = "../../../integrations/ax/README.md" if target == "README.md" else target
    path = "docs/features/integrations/index.md"
    source = f"[{name}]({target})\n"
    assert t.differences(source, source, source_path=path) == []
    assert t.differences(source, f"[名前]({target})\n", source_path=path)
    mixed = f"[{name} guide]({target})\n"
    assert t.differences(mixed, mixed, source_path=path)
    assert t.differences(mixed, f"[{name} ガイド]({target})\n", source_path=path) == []


@pytest.mark.parametrize(
    ("path", "label", "translated"),
    [
        ("docs/benchmarks/results.md", "MemoryAgentBench samples", "MemoryAgentBench サンプル"),
        (
            "docs/features/integrations/index.md",
            "GitHub Copilot CLI (experimental)",
            "GitHub Copilot CLI（実験的）",
        ),
        (
            "docs/features/integrations/frameworks.md",
            "Claude API memory tool",
            "Claude API メモリツール",
        ),
    ],
)
def test_names_do_not_exempt_one_word_qualifiers_or_mixed_cells(path, label, translated):
    source = _table(f"| {label} | | |")
    assert t.differences(source, source, source_path=path)
    assert t.differences(source, _table(f"| {translated} | | |"), source_path=path) == []


def test_returned_wire_values_are_required_even_without_backticks():
    path = "docs/reference/operations/calibration.md"
    source = _table('| `source` | `str` | "namespace" / "cluster" / "scalar" |')
    assert t.differences(source, source, source_path=path) == []
    native_quotes = source.replace(
        '"namespace" / "cluster" / "scalar"', "“namespace” / “cluster” / “scalar”"
    )
    assert t.differences(source, native_quotes, source_path=path) == []
    for replacement in [
        "命名空间级 / 集群级 / 标量",
        '"cluster" / "namespace" / "scalar"',
        '"namespace" / "cluster"',
        '"namespace" / "cluster" / "cluster"',
    ]:
        changed = source.replace('"namespace" / "cluster" / "scalar"', replacement)
        assert any(
            "protected source strings" in p
            for p in t.differences(source, changed, source_path=path)
        )


def test_escaped_and_inline_code_pipes_do_not_create_fake_english_cells():
    path = "docs/reference/operations/code.md"
    row = r"| `verdict` | `str` | gap \| pending \| covered \| not_affected \| unknown — the worst branch's word |"
    source = _table(row)
    target = _table(row.replace("the worst branch's word", "最も悪いブランチの判定"))
    assert t.differences(source, target, source_path=path) == []
    assert t.differences(source, target.replace("not_affected", "対象外"), source_path=path)
    union = _table(r"| `state` | `str \| None` | Install the package |")
    assert t.differences(union, union, source_path=path)


def test_policy_syntax_is_protected_but_its_explanation_is_translated():
    path = "docs/reference/operations/policy.md"
    row = r"| `condition` | `dict[str, Any]` | required | history condition: {op: formerly\|count_within\|not_since\|all\|any\|not, ...}; leaves take within ('1h'), operation, key (agent\|session\|user), success, args_equal |"
    header = "| Field | Type | Default | Meaning |\n| --- | --- | --- | --- |\n"
    source = header + row + "\n"
    target = (
        source.replace("required", "必須")
        .replace("history condition", "履歴条件")
        .replace("leaves take", "リーフの指定")
    )
    assert t.differences(source, target, source_path=path) == []
    assert t.differences(source, target.replace("args_equal", "引数一致"), source_path=path)
    assert t.differences(source, source, source_path=path)


def test_formula_exemption_is_limited_to_the_attested_operation_field():
    row = "| `fill_ratio` | `float` | len(content) / char_limit. |"
    source = _table(row)
    path = "docs/reference/operations/working_memory.md"
    assert t.differences(source, source, source_path=path) == []
    assert t.differences(source, source.replace(" / ", " * "), source_path=path)
    assert t.differences(source, source, source_path="docs/other.md")


def test_headerless_request_keeps_its_full_source_table_context():
    path = "docs/reference/operations/calibration.md"
    row = '| `source` | `str` | "namespace" / "cluster" / "scalar" |\n'
    english = _table("| `model` | `str` | Current model |\n" + row.rstrip())
    start = english.index(row)
    scope = SourceContext(path, english, start, start + len(row))
    assert t.differences(row, row, source_context=scope) == []
    bad = row.replace('"namespace" / "cluster" / "scalar"', "命名空间级 / 集群级 / 标量")
    assert t.differences(row, bad, source_context=scope)
    with pytest.raises(ValueError, match="does not match"):
        t.differences("wrong source", row, source_context=scope)


def test_preserved_name_does_not_spend_a_retry(monkeypatch):
    source = "## n8n\n"
    sent = []

    def reply(client, model, system, messages):
        sent.append(list(messages))
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": source}]}

    monkeypatch.setattr(t, "call", reply)
    scope = SourceContext("docs/features/integrations/frameworks.md", source, 0, len(source))
    assert (
        t.translate_text(
            source, t.LANGUAGES[1], None, t.Spend(t.DEFAULT_MODEL, 1), source_context=scope
        )
        == source
    )
    assert sent == [[{"role": "user", "content": source}]]


def test_a_changed_returned_literal_gets_precise_retry_feedback(monkeypatch):
    path = "docs/reference/operations/calibration.md"
    source = _table('| `source` | `str` | "namespace" / "cluster" / "scalar" |')
    replies = iter([source.replace('"namespace"', '"命名空间"'), source])
    sent = []

    def reply(client, model, system, messages):
        sent.append(list(messages))
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": next(replies)}]}

    monkeypatch.setattr(t, "call", reply)
    scope = SourceContext(path, source, 0, len(source))
    assert (
        t.translate_text(
            source, t.LANGUAGES[1], None, t.Spend(t.DEFAULT_MODEL, 1), source_context=scope
        )
        == source
    )
    assert len(sent) == 2
    assert "protected source strings" in sent[1][-1]["content"]
    assert "namespace" in sent[1][-1]["content"]


def test_assembly_refuses_a_changed_literal_before_creating_output(monkeypatch, tmp_path):
    path = "docs/reference/operations/calibration.md"
    source = _table('| `source` | `str` | "namespace" / "cluster" / "scalar" |')
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "prepare", lambda source_path, lang: (source, ""))
    monkeypatch.setattr(
        t, "translate_text", lambda text, *args, **kwargs: text.replace('"namespace"', '"命名空间"')
    )
    with pytest.raises(t.TranslationDiffers, match="protected source strings"):
        t.translate_page(path, t.LANGUAGES[1], None, t.Spend(t.DEFAULT_MODEL, 1))
    assert not (tmp_path / "i18n").exists()


def test_stored_page_check_uses_the_same_source_preservation_guard(monkeypatch, tmp_path):
    source_path = "docs/reference/operations/calibration.md"
    source = _table('| `source` | `str` | "namespace" / "cluster" / "scalar" |')
    lang = t.LANGUAGES[1]
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setattr(t, "sources", lambda: (source_path,))
    monkeypatch.setattr(t, "LANGUAGES", (lang,))
    monkeypatch.setattr(t, "prepare", lambda source_path, lang: (source, ""))
    output = tmp_path / t.target_path(lang.code, source_path)
    output.parent.mkdir(parents=True)
    output.write_text(source)
    assert t.untranslated_pages() == []
    output.write_text(source.replace('"namespace"', '"命名空间"'))
    assert any("protected source strings" in p for p in t.untranslated_pages())


@pytest.mark.parametrize(
    ("operation", "row", "english", "native", "literal"),
    [
        (
            "tracker",
            r"| `verdict` | `str` | planned \| answered \| empty \| unknown (no anchor to plan on) |",
            "no anchor to plan on",
            "計画の基準がない",
            "answered",
        ),
        (
            "observe",
            "| `kind` | `str` | file_edit / command_run / error / web_fetch / file_read / tool_use. |",
            "",
            "",
            "file_edit",
        ),
    ],
)
def test_attested_returned_vocabularies_preserve_values_and_translate_descriptions(
    operation, row, english, native, literal
):
    source = _table(row)
    output = source.replace(english, native) if english else source
    path = f"docs/reference/operations/{operation}.md"
    assert t.differences(source, output, source_path=path) == []
    assert t.differences(source, output.replace(literal, "値"), source_path=path)
    if english:
        assert t.differences(source, source, source_path=path)


def test_narrative_tier_descriptions_are_not_returned_wire_values():
    source = "## Scope (namespace, cluster, scalar)\n"
    output = "## 範囲（名前空間、クラスター、スカラー）\n"
    assert t.differences(source, output, source_path="docs/guides/debugging.md") == []


@pytest.mark.parametrize("particle", ["を", "の", "용", "的"])
@pytest.mark.parametrize("name,path", [("n8n", "frameworks"), ("Agent Substrate", "index")])
def test_native_particles_preserve_the_complete_name(particle, name, path):
    source = f"## {name}\n"
    assert (
        t.differences(
            source, f"## {name}{particle}\n", source_path=f"docs/features/integrations/{path}.md"
        )
        == []
    )
    assert t.differences(
        source, f"## {name}Custom\n", source_path=f"docs/features/integrations/{path}.md"
    )


@pytest.mark.parametrize("target", ["https://arbitrary.example/report.txt", "missing.txt"])
def test_a_remote_or_untracked_basename_does_not_attest_a_filename(target):
    name = target.rsplit("/", 1)[-1]
    source = f"[{name}]({target})\n"
    assert t.differences(source, source, source_path="docs/other.md")
    assert t.differences(source, f"[レポート]({target})\n", source_path="docs/other.md") == []


def test_canonical_repo_url_requires_the_actual_tracked_target():
    target = t.REPO_URL + "/blob/main/docs/features/integrations/claude-code.md"
    source = f"[claude-code.md]({target})\n"
    assert t.differences(source, source, source_path="docs/features/integrations/index.md") == []
    assert t.differences(
        source, f"[名前]({target})\n", source_path="docs/features/integrations/index.md"
    )
    wrong = source.replace("/blob/main/", "/blob/arbitrary/")
    assert t.differences(wrong, wrong, source_path="docs/features/integrations/index.md")


@pytest.mark.parametrize(
    "name,changed", [("n8n", "n8n_extra"), ("n8n", "n8né"), ("Mastra", "Mastração")]
)
def test_longer_unicode_or_latin_identifiers_do_not_preserve_a_name(name, changed):
    assert t.differences(
        f"## {name}\n", f"## {changed}\n", source_path="docs/features/integrations/frameworks.md"
    )


_MODEL_PATH = "docs/features/memory/conflicts-and-refusal.md"
_MODEL_HEADER = "| Setting | Default | Effect |\n| --- | --- | --- |\n"
_MODEL_ROW = "| `LOOM_CONFLICT_RESOLVER_MODEL` | gpt-6.1-sol | The resolver's judge. |\n"


def test_model_default_preserves_its_attested_setting_cell():
    source = _MODEL_HEADER + _MODEL_ROW
    output = source.replace("The resolver's judge.", "判定するモデル。")
    assert t.differences(source, output, source_path=_MODEL_PATH) == []
    assert t.differences(source, source, source_path=_MODEL_PATH)
    assert any(
        "protected source strings" in problem
        for problem in t.differences(
            source, output.replace("gpt-6.1-sol", "gpt-6.1-terra"), source_path=_MODEL_PATH
        )
    )


@pytest.mark.parametrize("case", ["path", "setting", "column", "model", "prose"])
def test_model_defaults_do_not_exempt_other_source_nodes(case):
    source = _MODEL_HEADER + _MODEL_ROW
    path = _MODEL_PATH
    if case == "path":
        path = "docs/other.md"
    elif case == "setting":
        source = source.replace("LOOM_CONFLICT_RESOLVER_MODEL", "LOOM_OTHER_MODEL")
    elif case == "column":
        source = _MODEL_HEADER + "| `LOOM_CONFLICT_RESOLVER_MODEL` | 0 | gpt-6.1-sol |\n"
    elif case == "model":
        source = source.replace("gpt-6.1-sol", "gpt-6.1-terra")
    else:
        source = "## gpt-6.1-sol\n"
    output = source.replace("The resolver's judge.", "判定するモデル。")
    assert t.differences(source, output, source_path=path)


def test_a_model_default_does_not_exempt_its_ordinary_qualifier():
    source = (_MODEL_HEADER + _MODEL_ROW).replace("gpt-6.1-sol", "gpt-6.1-sol recommended")
    output = source.replace("The resolver's judge.", "判定するモデル。")
    assert t.differences(source, output, source_path=_MODEL_PATH)
    assert (
        t.differences(source, output.replace("recommended", "推奨"), source_path=_MODEL_PATH) == []
    )


def test_a_model_default_cannot_move_to_another_column_or_setting():
    source = _MODEL_HEADER + _MODEL_ROW + "| `LOOM_OTHER_MODEL` | 0 | Another model. |\n"
    output = source.replace("The resolver's judge.", "判定するモデル。").replace(
        "Another model.", "別のモデル。"
    )
    moved_column = output.replace(
        "gpt-6.1-sol | 判定するモデル。", "判定するモデル。 | gpt-6.1-sol"
    )
    moved_setting = (
        output.replace("LOOM_CONFLICT_RESOLVER_MODEL", "SETTING_PLACEHOLDER")
        .replace("LOOM_OTHER_MODEL", "LOOM_CONFLICT_RESOLVER_MODEL")
        .replace("SETTING_PLACEHOLDER", "LOOM_OTHER_MODEL")
    )
    for changed in (moved_column, moved_setting):
        assert any(
            "protected source strings" in problem
            for problem in t.differences(source, changed, source_path=_MODEL_PATH)
        )


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
def test_a_headerless_model_default_request_keeps_its_setting_context(newline):
    english = (_MODEL_HEADER + _MODEL_ROW).replace("\n", newline)
    row = _MODEL_ROW.rstrip("\n").replace("\n", newline)
    start = english.index(row)
    scope = SourceContext(_MODEL_PATH, english, start, start + len(row))
    output = row.replace("The resolver's judge.", "判定するモデル。")
    assert t.differences(row, output, source_context=scope) == []
    assert t.differences(row, output.replace("gpt-6.1-sol", "別のモデル"), source_context=scope)
