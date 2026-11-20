from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts.i18n import chunks, lifecycle
from scripts.i18n import translate as t


class TableAnalysisTests(TestCase):
    def test_page_validation_parses_each_table_dialect_once(self) -> None:
        source = "docs/table.md"
        english = "| Name | Value |\n| --- | --- |\n| color | blue |\n"
        translated = "| 項目 | 値 |\n| --- | --- |\n| 色 | 青 |\n"
        calls: Counter[str] = Counter()
        site_tables = chunks._site_tables
        github_parse = chunks.MarkdownIt.parse

        def site(text: str):
            calls["site"] += 1
            return site_tables(text)

        def github(parser, text, env=None):
            calls["github"] += 1
            return github_parse(parser, text, env)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / source
            path.parent.mkdir()
            path.write_text(english)
            with (
                patch.multiple(
                    t,
                    ROOT=root,
                    sources=lambda: (source,),
                    blob=lambda _: "a" * 40,
                    # Request assembly has separate parser checks.
                    english_requests=lambda text: [text],
                ),
                patch.object(chunks, "_site_tables", site),
                patch.object(chunks.MarkdownIt, "parse", github),
            ):
                lang = next(lang for lang in t.LANGUAGES if lang.code == "ja")
                text = t.header(source, lang, t.DEFAULT_MODEL, "a" * 40) + translated
                self.assertEqual(lifecycle.candidate_problems(f"i18n/ja/{source}", text), [])
                self.assertEqual(calls, {"site": 2, "github": 2})
                calls.clear()
                for _ in range(2):
                    chunks.table_labels(english)
                    chunks.table_shape(english)
                self.assertEqual(calls, {"site": 4, "github": 4})

    def test_shared_analysis_keeps_duplicate_row_offsets(self) -> None:
        row = "| color | blue |\n"
        text = "| Name | Value |\n| --- | --- |\n" + row * 2
        analysis = chunks.table_analysis(text)
        self.assertEqual(analysis.labels, chunks.table_labels(text))
        self.assertEqual(analysis.shape, chunks.table_shape(text))
        self.assertEqual(analysis.labels[("cell", 1, 0)].start, text.index(row))
        self.assertEqual(analysis.labels[("cell", 2, 0)].start, text.rindex(row))
        pipe_text = text.replace("blue", "`blue\\|green`")
        pipe_analysis = chunks.table_analysis(pipe_text)
        self.assertEqual(pipe_analysis.labels, chunks.table_labels(pipe_text))
        self.assertEqual(pipe_analysis.shape, chunks.table_shape(pipe_text))
        # Rows without matching dialect source maps keep page-wide obligations.
        self.assertEqual(pipe_analysis.labels[("cell", 1, 0)].start, 0)
        self.assertEqual(pipe_analysis.labels[("cell", 1, 0)].end, len(pipe_text))

    def test_page_comparison_keeps_table_containers_and_translated_labels(self) -> None:
        english = (
            "| Property name | Current value |\n| --- | --- |\n| Visible color | Bright blue |\n"
        )
        translated = "| 項目 | 値 |\n| --- | --- |\n| 色 | 青 |\n"
        prefix = "Before the table.\n\n"
        scope = t.context.SourceContext(
            "docs/table.md",
            prefix + english + "\nAfter the table.\n",
            len(prefix),
            len(prefix + english),
        )
        self.assertEqual(t.differences(english, translated, source_context=scope), [])
        for prefix, indent in (("", ""), ("", "> "), ("!!! note\n\n", "    ")):
            with self.subTest(indent=indent):
                source = prefix + "".join(indent + line for line in english.splitlines(True))
                target = prefix + "".join(indent + line for line in translated.splitlines(True))
                self.assertEqual(
                    t.page_differences(source, target, source_path="docs/table.md"), []
                )
                self.assertTrue(
                    any(
                        "left in English" in problem
                        for problem in t.page_differences(
                            source, source, source_path="docs/table.md"
                        )
                    )
                )
                shortened = prefix + "".join(
                    indent + line for line in translated.splitlines(True)[:2]
                )
                self.assertIn(
                    "table row/cell layout differs after request assembly",
                    t.page_differences(source, shortened, source_path="docs/table.md"),
                )
