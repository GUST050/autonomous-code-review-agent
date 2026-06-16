"""
Tests for main.py helper functions — no LLM calls, no graph execution.
"""
import os
from unittest.mock import patch, MagicMock
import pytest

from schemas.fix_response import FixResponse
from config import ModelConfig, CLAUDE_HAIKU, CLAUDE_SONNET, GPT4O_MINI
from main import _save_output, _prompt_accept_fixes, _build_llm


class TestSaveOutput:
    def test_saves_fixed_code_to_file(self, tmp_path):
        path = str(tmp_path / "fixed.py")
        state = {"fix_result": FixResponse(fixed_code="x = 1\n", changes=[])}
        _save_output(state, path)
        assert open(path).read() == "x = 1\n"

    def test_creates_nested_directories(self, tmp_path):
        path = str(tmp_path / "subdir" / "nested" / "fixed.py")
        state = {"fix_result": FixResponse(fixed_code="y = 2\n", changes=[])}
        _save_output(state, path)
        assert os.path.exists(path)

    def test_does_not_create_file_when_no_fix_result(self, tmp_path):
        path = str(tmp_path / "should_not_exist.py")
        _save_output({}, path)
        assert not os.path.exists(path)

    def test_does_not_create_file_when_fixed_code_is_empty(self, tmp_path):
        path = str(tmp_path / "should_not_exist.py")
        state = {"fix_result": FixResponse(fixed_code="", changes=[])}
        _save_output(state, path)
        assert not os.path.exists(path)


class TestPromptAcceptFixes:
    def _state(self, changes=None):
        return {"fix_result": FixResponse(fixed_code="x = 1\n", changes=changes or [])}

    def test_does_nothing_when_no_fix_result(self, capsys):
        _prompt_accept_fixes({}, output_path=None)
        assert capsys.readouterr().out == ""

    def test_does_nothing_when_fixed_code_empty(self, capsys):
        _prompt_accept_fixes({"fix_result": FixResponse(fixed_code="", changes=[])}, None)
        assert capsys.readouterr().out == ""

    def test_yes_saves_file(self, tmp_path):
        path = str(tmp_path / "out.py")
        with patch("builtins.input", return_value="yes"):
            _prompt_accept_fixes(self._state(["Replace MD5 with SHA-256"]), path)
        assert os.path.exists(path)

    def test_no_does_not_save_file(self, tmp_path):
        path = str(tmp_path / "out.py")
        with patch("builtins.input", return_value="no"):
            _prompt_accept_fixes(self._state(["Replace MD5 with SHA-256"]), path)
        assert not os.path.exists(path)

    def test_single_prompt_regardless_of_change_count(self):
        """input() must be called exactly once no matter how many changes there are."""
        with patch("builtins.input", return_value="no") as mock_input:
            _prompt_accept_fixes(self._state(["change 1", "change 2", "change 3"]), None)
        mock_input.assert_called_once()

    def test_yes_without_output_path_prints_message(self, capsys):
        with patch("builtins.input", return_value="y"):
            _prompt_accept_fixes(self._state(["fix X"]), output_path=None)
        assert "--output PATH" in capsys.readouterr().out

    def test_no_prints_discarded(self, capsys):
        with patch("builtins.input", return_value="n"):
            _prompt_accept_fixes(self._state(["fix X"]), output_path=None)
        assert "discarded" in capsys.readouterr().out.lower()

    def test_changes_listed_before_prompt(self, capsys):
        with patch("builtins.input", return_value="no"):
            _prompt_accept_fixes(self._state(["Replace MD5", "Use parameterized queries"]), None)
        out = capsys.readouterr().out
        assert "Replace MD5" in out
        assert "Use parameterized queries" in out


class TestBuildLlm:
    def test_anthropic_config_returns_chat_anthropic(self):
        with patch("review.ChatAnthropic") as mock:
            _build_llm(CLAUDE_HAIKU)
        mock.assert_called_once_with(
            model=CLAUDE_HAIKU.model,
            temperature=CLAUDE_HAIKU.temperature,
            max_tokens=CLAUDE_HAIKU.max_tokens,
        )

    def test_openai_config_returns_chat_openai(self):
        with patch("review.ChatOpenAI") as mock:
            _build_llm(GPT4O_MINI)
        mock.assert_called_once_with(
            model=GPT4O_MINI.model,
            temperature=GPT4O_MINI.temperature,
            max_tokens=GPT4O_MINI.max_tokens,
        )

    def test_unknown_provider_falls_back_to_openai(self):
        cfg = ModelConfig(
            model="unknown-model",
            provider="unknown",
            temperature=0.0,
            max_tokens=100,
            input_cost_per_million=1.0,
            output_cost_per_million=1.0,
        )
        with patch("review.ChatOpenAI") as mock:
            _build_llm(cfg)
        mock.assert_called_once()


class TestModelConfigProvider:
    def test_all_configs_have_provider(self):
        for cfg in [CLAUDE_HAIKU, CLAUDE_SONNET, GPT4O_MINI]:
            assert cfg.provider in ("anthropic", "openai")

    def test_claude_configs_are_anthropic(self):
        assert CLAUDE_HAIKU.provider == "anthropic"
        assert CLAUDE_SONNET.provider == "anthropic"

    def test_gpt_is_openai(self):
        assert GPT4O_MINI.provider == "openai"
