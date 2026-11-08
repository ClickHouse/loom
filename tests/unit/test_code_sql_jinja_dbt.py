"""Jinja-templated SQL — line-preserving neutralization, dbt
models and macros as symbols, ref / source / config facets, model → model
and macro → macro edges."""

from __future__ import annotations

import pytest

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract import lang_sql as ls
from server.code.domain.extract import lang_sql_facets
from server.code.domain.extract import lang_sql_jinja as lj
from server.code.domain.extract.lang_python import module_scope
from tests.unit.code_fixtures import grows_linearly

_J2 = """\
-- Tags: long
{% for algo in ['hash', 'partial_merge'] -%}
SET join_algorithm = '{{ algo }}';
SELECT * FROM t1 ANY LEFT JOIN t2 USING (k) FORMAT {{ fmt }};
{%- endfor %}
{# a comment; with a semicolon #}
SELECT 1;
"""

_MODEL = """\
{{
    config(
        materialized='incremental',
        tags=['finance', "daily"]
    )
}}

with orders as (
    select * from {{ ref('stg_orders') }}
),
payments as (
    select * from {{ ref('jaffle_shop', 'stg_payments') }}
),
raw as (
    select * from {{ source('jaffle', 'customers') }}
)
select {{ dbt_utils.star(from=ref('stg_orders')) }}, {{ money(amount) }}
from orders
{% if is_incremental() %}
where order_date > (select max(order_date) from {{ this }})
{% endif %}
"""

_MACROS = """\
{% macro money(col) -%}
    round({{ col }}, 2)
{%- endmacro %}

{% test positive(model, column_name) %}
select * from {{ model }} where {{ column_name }} <= 0
{% endtest %}

{% macro uses_money(col) %}
  {{ money(col) }} + {{ dbt_utils.safe_add([col]) }}
{% endmacro %}
"""


def test_neutralize_keeps_lines_and_blanks_the_template() -> None:
    out = lj.neutralize(_J2)
    assert out.count("\n") == _J2.count("\n")
    lines = out.split("\n")
    assert lines[0] == "-- Tags: long" and lines[1] == "" and lines[4] == ""
    assert lines[2] == "SET join_algorithm = '__jinja__';"
    assert lines[3] == "SELECT * FROM t1 ANY LEFT JOIN t2 USING (k) FORMAT __jinja__;"
    assert lines[5] == "" and lines[6] == "SELECT 1;"
    assert lj.neutralize("{% raw %}SELECT '{{ x }}';{% endraw %}") == "SELECT '{{ x }}';"


def test_clickhouse_j2_test_splits_as_sql_not_as_template() -> None:
    sts, ju = ls.split_file("tests/queries/0_stateless/01000_join.sql.j2", _J2)
    assert ju.present and not ju.dbt and ju.symbols == []
    assert [(s.kind, s.start_line, s.end_line) for s in sts] == [
        ("SET", 3, 3),
        ("SELECT", 4, 4),
        ("SELECT", 7, 7),
    ]  # no FOR / ENDFOR records; the header line and the blanked template lines belong to nobody
    facets = {(f.kind, f.name) for s in sts for f in lang_sql_facets.statement_facets(s)}
    assert ("setting", "join_algorithm") in facets
    assert not any("__jinja__" in n for _, n in facets)  # FORMAT __jinja__ is no format


def test_dbt_model_symbol_refs_sources_config_and_calls() -> None:
    path = "models/marts/fct_orders.sql"
    ju = lj.jinja_units(path, module_scope(path), _MODEL)
    assert ju.dbt and ju.model == "fct_orders"
    assert [(s.name, s.kind, s.start_line, s.end_line) for s in ju.symbols] == [
        ("fct_orders", "model", 1, _MODEL.count("\n") + 1),
    ]
    assert ju.symbols[0].signature == "model fct_orders · materialized: incremental"
    assert ju.symbols[0].qualified_name == "models.marts.fct_orders::fct_orders"
    assert ju.refs == ["stg_orders", "stg_payments"]
    facets = {(f.kind, f.name) for f in ju.facets}
    assert {
        ("materialized", "incremental"),
        ("tag", "finance"),
        ("tag", "daily"),
        ("table_read", "stg_orders"),
        ("table_read", "stg_payments"),
        ("source", "jaffle.customers"),
        ("table_read", "customers"),
        ("macro_call", "star"),
        ("macro_call", "money"),
    } <= facets
    assert "is_incremental" not in ju.macro_calls and "config" not in ju.macro_calls
    assert ju.ref_lines["stg_orders"] == 9 and ju.call_lines["money"] == 17


def test_macro_file_symbols_and_own_calls_excluded() -> None:
    path = "macros/money.sql"
    ju = lj.jinja_units(path, module_scope(path), _MACROS)
    assert ju.dbt and ju.model == ""  # a macro file is not a model
    assert [(s.name, s.kind, s.start_line, s.end_line, s.signature) for s in ju.symbols] == [
        ("money", "macro", 1, 3, "macro money(col)"),
        ("positive", "macro", 5, 7, "test positive(model, column_name)"),
        ("uses_money", "macro", 9, 11, "macro uses_money(col)"),
    ]
    assert ju.macro_calls == ["safe_add"]  # `money` is this file's own; `round` is a builtin


def test_edges_bind_refs_to_models_and_calls_to_macros_from_the_enclosing_symbol() -> None:
    mpath, macpath = "models/marts/fct_orders.sql", "macros/money.sql"
    model = lj.jinja_units(mpath, module_scope(mpath), _MODEL)
    macros = lj.jinja_units(macpath, module_scope(macpath), _MACROS)
    stg = lj.jinja_units(
        "models/staging/stg_orders.sql",
        "models.staging.stg_orders",
        "{{ config(materialized='view') }}\nselect * from {{ source('a', 'o') }}",
    )
    table = {}
    for ju in (model, macros, stg):
        for s in ju.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    edges = lj.jinja_edges(mpath, model, table)
    got = sorted((e.from_qualified_name, e.to_qualified_name, e.line) for e in edges)
    assert got == [
        ("models.marts.fct_orders::fct_orders", "macros.money::money", 17),
        ("models.marts.fct_orders::fct_orders", "models.staging.stg_orders::stg_orders", 9),
    ]  # stg_payments and dbt_utils.star are not defined in the tree: no edge, no miss
    macro_edges = lj.jinja_edges(macpath, macros, table)
    assert macro_edges == []  # safe_add is a package macro the tree does not hold
    assert all(e.edge_kind == "calls" and e.confidence == 0.9 for e in edges)


def test_parse_file_carries_model_and_macro_symbols(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_sql_statement_records", True, raising=False)
    pf = parse_file("models/marts/fct_orders.sql", _MODEL.encode())
    assert pf is not None and pf.lang == "sql"
    assert [s.kind for s in pf.symbols] == ["statement", "model"]
    units = ls.sql_units(pf)
    assert {(f.kind, f.name) for f in units.facets} >= {
        ("table_read", "stg_orders"),
        ("materialized", "incremental"),
    }
    assert units.chunks[-1].content.startswith("{{\n    config(")  # chunk text is the original
    pf = parse_file("macros/money.sql", _MACROS.encode())
    assert pf is not None and [s.name for s in pf.symbols if s.kind == "macro"] == [
        "money",
        "positive",
        "uses_money",
    ]


def test_a_plain_sql_file_under_models_is_a_model() -> None:
    """dbt-utils' integration tests: `models/sql/test_x.sql` with no Jinja at all,
    or only a macro call, is a model — the directory is dbt's convention."""
    ju = lj.jinja_units(
        "integration_tests/models/sql/test_plain.sql",
        "integration_tests.models.sql.test_plain",
        "select 1 as id\n",
    )
    assert ju.dbt and [s.kind for s in ju.symbols] == ["model"] and not ju.present
    ju = lj.jinja_units(
        "models/x.sql", "models.x", "select {{ safe_cast('a', type_string()) }} as c"
    )
    assert ju.dbt and ju.present and ju.model == "x"
    assert lj.jinja_units("sql/updates/x.sql", "sql.updates.x", "select 1").dbt is False


@pytest.mark.parametrize(
    "unit",
    [
        "{% macro a() %}",
        "{{ a<i>() }}\n{{ ref('m<i>') }}\n",
        "{%",
        "{#",
        "{% raw %}",
        "{{config(",
    ],
)
def test_a_dbt_file_of_many_tags_reads_in_linear_time(unit: str) -> None:
    def run(n: int) -> None:
        text = "".join(unit.replace("<i>", str(i)) for i in range(n))
        units = lj.jinja_units("models/x.sql", "m", text)
        lj.jinja_edges("models/x.sql", units, {})

    grows_linearly(run, 2_000, 8_000)


def test_an_unclosed_statement_tag_names_no_macro_call() -> None:
    for tail in ("> 0\n", "> {{ var('n') }}"):  # the second ends the file in `}}`
        text = "{{ dollars(x) }}\nselect a from t where b like '{%'\nand coalesce(c, 0) " + tail
        assert lj._calls(text, set(), lj._newlines(text)) == {"dollars": 1}, tail
