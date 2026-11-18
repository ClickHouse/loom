"""Current-input corrections are part of the canonical request, without old replies."""

import pytest

from scripts.i18n import translate as t
from scripts.i18n.context import SourceContext

SOURCE = "Use the `keeper` backend.\n"
REPLY = "使用 `keeper` 后端。\n"
FEEDBACK = "Keep the source's backend choice and negation.\nTranslate ordinary annotations."


def _scope():
    return SourceContext("docs/guide.md", SOURCE, 0, len(SOURCE))


def _record(monkeypatch, replies=(REPLY,)):
    answers, sent = iter(replies), []

    def call(client, model, system, messages):
        sent.append((system, list(messages)))
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": next(answers)}]}

    monkeypatch.setattr(t, "call", call)
    return sent


def test_empty_feedback_keeps_existing_canonical_system_and_messages(monkeypatch):
    sent = _record(monkeypatch)
    lang = t.LANGUAGES[0]
    assert t.request_system(lang) == t.SYSTEM.format(language=lang.english, style=lang.style)
    assert t.translate_text(SOURCE, lang, None, t.Spend(t.DEFAULT_MODEL, 1)) == REPLY
    assert sent == [(t.request_system(lang), [{"role": "user", "content": SOURCE}])]


def test_current_feedback_is_received_exactly_in_the_actual_system(monkeypatch):
    sent = _record(monkeypatch)
    lang = t.LANGUAGES[0]
    assert (
        t.translate_text(
            SOURCE,
            lang,
            None,
            t.Spend(t.DEFAULT_MODEL, 1),
            source_context=_scope(),
            initial_feedback=FEEDBACK,
        )
        == REPLY
    )
    actual_system, messages = sent[0]
    assert actual_system == t.request_system(lang, initial_feedback=FEEDBACK)
    assert actual_system.startswith(t.request_system(lang) + "\n\n")
    assert actual_system.endswith("\n" + FEEDBACK)
    assert messages == [{"role": "user", "content": SOURCE}]
    assert t.request_system(lang, initial_feedback=FEEDBACK + "!") != actual_system


def test_mechanical_retry_echoes_only_the_new_current_input_reply(monkeypatch):
    bad = REPLY.replace("`keeper`", "`log`")
    sent = _record(monkeypatch, (bad, REPLY))
    lang = t.LANGUAGES[0]
    assert (
        t.translate_text(
            SOURCE,
            lang,
            None,
            t.Spend(t.DEFAULT_MODEL, 1),
            source_context=_scope(),
            initial_feedback=FEEDBACK,
        )
        == REPLY
    )
    assert len(sent) == 2
    assert sent[0][0] == sent[1][0] == t.request_system(lang, initial_feedback=FEEDBACK)
    assert sent[0][1] == [{"role": "user", "content": SOURCE}]
    assert sent[1][1][:2] == [
        {"role": "user", "content": SOURCE},
        {"role": "assistant", "content": bad},
    ]
    assert sent[1][1][-1]["role"] == "user"
    assert "inline code" in sent[1][1][-1]["content"]


@pytest.mark.parametrize(
    "feedback",
    ["x" * 4097, "非 ASCII", "a\x00b", "a\tb", "a\rb", "a\x7fb"],
    ids=["oversize", "nonascii", "nul", "tab", "cr", "del"],
)
def test_invalid_feedback_is_rejected_before_spend_or_provider(monkeypatch, feedback):
    sent = _record(monkeypatch)
    spend = t.Spend(t.DEFAULT_MODEL, 0)
    with pytest.raises(ValueError, match="feedback"):
        t.translate_text(
            SOURCE, t.LANGUAGES[0], None, spend, source_context=_scope(), initial_feedback=feedback
        )
    assert sent == []


def test_exact_feedback_byte_limit_is_allowed():
    feedback = "x" * 4096
    assert t.request_system(t.LANGUAGES[0], initial_feedback=feedback).endswith(feedback)


def test_nonempty_feedback_requires_current_source_context_before_spend(monkeypatch):
    sent = _record(monkeypatch)
    with pytest.raises(ValueError, match="source context"):
        t.translate_text(
            SOURCE, t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 0), initial_feedback=FEEDBACK
        )
    assert not sent


@pytest.mark.parametrize("feedback", [FEEDBACK, ""], ids=["feedback", "default"])
@pytest.mark.parametrize(
    "start,end", [(0, len(SOURCE) - 1), (-1, len(SOURCE)), (0, len(SOURCE) + 1)]
)
def test_mismatched_context_fails_before_spend_or_provider(monkeypatch, start, end, feedback):
    sent = _record(monkeypatch)
    scope = SourceContext("docs/guide.md", SOURCE, start, end)
    with pytest.raises(ValueError, match="source context"):
        t.translate_text(
            SOURCE,
            t.LANGUAGES[0],
            None,
            t.Spend(t.DEFAULT_MODEL, 0),
            source_context=scope,
            initial_feedback=feedback,
        )
    assert not sent


def test_feedback_cannot_use_an_unnamed_source_context(monkeypatch):
    sent = _record(monkeypatch)
    with pytest.raises(ValueError, match="source context"):
        t.translate_text(
            SOURCE,
            t.LANGUAGES[0],
            None,
            t.Spend(t.DEFAULT_MODEL, 0),
            source_context=SourceContext("", SOURCE, 0, len(SOURCE)),
            initial_feedback=FEEDBACK,
        )
    assert not sent
