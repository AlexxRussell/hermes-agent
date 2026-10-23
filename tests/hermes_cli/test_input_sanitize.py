"""Tests for shared user prompt input sanitization."""

import pytest

from hermes_cli.input_sanitize import (
    collapse_repeated_input_artifacts,
    sanitize_user_prompt_text,
    strip_leaked_bracketed_paste_wrappers,
)


class TestStripLeakedBracketedPasteWrappers:
    def test_plain_text_unchanged(self):
        assert strip_leaked_bracketed_paste_wrappers("hello world") == "hello world"



    def test_does_not_strip_non_wrapper_bracket_forms_in_normal_text(self):
        text = "literal[200~tag and literal[201~tag should stay"
        assert strip_leaked_bracketed_paste_wrappers(text) == text


class TestCollapseRepeatedInputArtifacts:
    def test_issue_62557_corruption_tail(self):
        prefix = "需要时随时叫我。"
        tail = "[e~[[e" + "~[[e" * 20
        assert collapse_repeated_input_artifacts(prefix + tail) == prefix


    def test_trailing_punctuation_preserved(self):
        assert collapse_repeated_input_artifacts("wait....") == "wait...."


class TestSanitizeUserPromptText:
    @pytest.mark.parametrize("text", [
        "12:00~12:15",
        "08:00~09:30",
        "12:01~ 12:15",
        "12:01~",
        "Schedule lunch 12:00~12:15.",
        "Meet (12:00~12:01~).",
    ])
    def test_preserves_time_literals(self, text):
        assert sanitize_user_prompt_text(text) == text

    @pytest.mark.parametrize(("text", "expected"), [
        ("00~hello01~", "hello"),
        ("prefix:00~hello01~ suffix", "prefix:hello suffix"),
        ("00~12:00~12:15\n12:01~ 12:1501~", "12:00~12:15\n12:01~ 12:15"),
        ("\x1b[200~12:00~12:15\x1b[201~", "12:00~12:15"),
    ])
    def test_strips_paste_wrappers_without_corrupting_their_content(self, text, expected):
        assert sanitize_user_prompt_text(text) == expected

    def test_combines_wrapper_strip_and_tail_collapse(self):
        prefix = "hello["
        corrupted = prefix + "~[[e" * 8
        assert sanitize_user_prompt_text(corrupted) == "hello"
