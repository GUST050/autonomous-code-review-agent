"""
Tests for BaseAgent retry and rate-limit backoff logic.

No real LLM calls — structured_llm.invoke is patched.
"""
from unittest.mock import MagicMock, patch, call

import pytest

from agents.base_agent import BaseAgent, _call_with_timeout, _is_rate_limit


# ── Minimal concrete subclass ─────────────────────────────────────────────────

class _Agent(BaseAgent):
    def get_system_prompt(self) -> str:
        return "system prompt"


def _make_agent() -> _Agent:
    return _Agent(llm=MagicMock(), name="TestAgent")


# ── _is_rate_limit ─────────────────────────────────────────────────────────────

class TestIsRateLimit:
    def test_http_429_in_message(self):
        assert _is_rate_limit(Exception("HTTP/1.1 429 Too Many Requests"))

    def test_rate_limit_phrase(self):
        assert _is_rate_limit(Exception("rate limit exceeded"))

    def test_ratelimit_no_space(self):
        assert _is_rate_limit(Exception("RateLimitError: quota exceeded"))

    def test_classname_ratelimiterror(self):
        class RateLimitError(Exception):
            pass
        assert _is_rate_limit(RateLimitError("quota"))

    def test_generic_error_not_rate_limit(self):
        assert not _is_rate_limit(Exception("invalid structured output"))

    def test_auth_error_not_rate_limit(self):
        assert not _is_rate_limit(Exception("401 Unauthorized"))

    def test_500_error_not_rate_limit(self):
        assert not _is_rate_limit(Exception("500 Internal Server Error"))


# ── Retry on rate limit ───────────────────────────────────────────────────────

class TestRetryBackoff:
    def test_succeeds_on_first_attempt_with_no_retries(self):
        agent = _make_agent()
        expected = MagicMock()
        with patch.object(
            agent.llm, "with_structured_output", return_value=MagicMock(invoke=MagicMock(return_value=expected))
        ):
            result = agent.invoke("hello", max_retries=3)
        assert result is expected

    def test_retries_on_rate_limit_then_succeeds(self):
        agent = _make_agent()
        success_value = MagicMock()
        invoke_mock = MagicMock(side_effect=[
            Exception("429 Too Many Requests"),
            success_value,
        ])
        with patch.object(
            agent.llm, "with_structured_output", return_value=MagicMock(invoke=invoke_mock)
        ), patch("agents.base_agent.time.sleep") as mock_sleep:
            result = agent.invoke("hello", max_retries=3)

        assert result is success_value
        assert mock_sleep.call_count == 1
        delay = mock_sleep.call_args[0][0]
        assert 2.0 <= delay <= 4.0  # base=2, jitter up to 1, attempt 1

    def test_backoff_doubles_on_second_rate_limit(self):
        agent = _make_agent()
        success_value = MagicMock()
        invoke_mock = MagicMock(side_effect=[
            Exception("429 Too Many Requests"),
            Exception("429 Too Many Requests"),
            success_value,
        ])
        with patch.object(
            agent.llm, "with_structured_output", return_value=MagicMock(invoke=invoke_mock)
        ), patch("agents.base_agent.time.sleep") as mock_sleep:
            result = agent.invoke("hello", max_retries=4)

        assert result is success_value
        assert mock_sleep.call_count == 2
        delay_1 = mock_sleep.call_args_list[0][0][0]
        delay_2 = mock_sleep.call_args_list[1][0][0]
        # Second delay must be roughly double the first (before jitter)
        assert delay_2 > delay_1

    def test_no_sleep_on_generic_error(self):
        agent = _make_agent()
        success_value = MagicMock()
        invoke_mock = MagicMock(side_effect=[
            Exception("invalid JSON output"),
            success_value,
        ])
        with patch.object(
            agent.llm, "with_structured_output", return_value=MagicMock(invoke=invoke_mock)
        ), patch("agents.base_agent.time.sleep") as mock_sleep:
            agent.invoke("hello", max_retries=3)

        mock_sleep.assert_not_called()

    def test_exhausts_retries_and_returns_error_response(self):
        agent = _make_agent()
        invoke_mock = MagicMock(side_effect=Exception("429 Too Many Requests"))
        with patch.object(
            agent.llm, "with_structured_output", return_value=MagicMock(invoke=invoke_mock)
        ), patch("agents.base_agent.time.sleep"):
            result = agent.invoke("hello", max_retries=3)

        # After exhausting retries, returns fallback AgentResponse with no findings
        assert result.severity == 0
        assert result.findings == []
        assert "429" in result.reasoning or "attempts" in result.reasoning

    def test_backoff_capped_at_max_delay(self):
        """Very high attempt count must not exceed _RATE_LIMIT_MAX_DELAY."""
        from agents.base_agent import _RATE_LIMIT_BASE_DELAY, _RATE_LIMIT_MAX_DELAY
        import random as _random

        agent = _make_agent()
        # Simulate attempt 10 — 2.0 * 2^9 = 1024s, must be capped at 60s
        side_effects = [Exception("429 Too Many Requests")] * 10 + [MagicMock()]
        invoke_mock = MagicMock(side_effect=side_effects)

        recorded_delays = []
        original_sleep = __import__("time").sleep

        def capture_sleep(delay):
            recorded_delays.append(delay)

        with patch.object(
            agent.llm, "with_structured_output", return_value=MagicMock(invoke=invoke_mock)
        ), patch("agents.base_agent.time.sleep", side_effect=capture_sleep):
            agent.invoke("hello", max_retries=11)

        assert all(d <= _RATE_LIMIT_MAX_DELAY + 1 for d in recorded_delays), (
            f"A delay exceeded the cap: {recorded_delays}"
        )


# ── _call_with_timeout ───────────────────────────────────────────────────────

class TestCallWithTimeout:
    def test_fast_call_returns_value(self):
        result = _call_with_timeout(lambda: 42, timeout_seconds=5)
        assert result == 42

    def test_raises_timeout_error_when_future_times_out(self):
        import concurrent.futures as _cf
        with patch("agents.base_agent.concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
            mock_future = MagicMock()
            mock_future.result.side_effect = _cf.TimeoutError()
            mock_pool = MagicMock()
            mock_pool.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.submit.return_value = mock_future
            mock_pool_cls.return_value = mock_pool

            with pytest.raises(TimeoutError, match="timed out after 30s"):
                _call_with_timeout(lambda: None, timeout_seconds=30)

    def test_non_timeout_exception_propagates_unchanged(self):
        def boom():
            raise ValueError("unexpected error")

        with pytest.raises(ValueError, match="unexpected error"):
            _call_with_timeout(boom, timeout_seconds=5)


class TestTimeoutRetry:
    def test_timeout_triggers_retry_without_sleep(self):
        """TimeoutError must retry immediately — no backoff sleep."""
        agent = _make_agent()
        success_value = MagicMock()

        with patch("agents.base_agent._call_with_timeout") as mock_call:
            mock_call.side_effect = [TimeoutError("timed out after 60s"), success_value]
            with patch("agents.base_agent.time.sleep") as mock_sleep:
                result = agent.invoke("hello", max_retries=3)

        assert result is success_value
        mock_sleep.assert_not_called()

    def test_timeout_exhausts_retries_and_returns_error_response(self):
        agent = _make_agent()

        with patch("agents.base_agent._call_with_timeout", side_effect=TimeoutError("timed out")):
            result = agent.invoke("hello", max_retries=3)

        assert result.severity == 0
        assert result.findings == []
        assert "timed out" in result.reasoning.lower() or "attempts" in result.reasoning.lower()

    def test_timeout_warning_logged(self):
        agent = _make_agent()
        success_value = MagicMock()

        with patch("agents.base_agent._call_with_timeout") as mock_call:
            mock_call.side_effect = [TimeoutError("timed out after 60s"), success_value]
            with patch("agents.base_agent.logger") as mock_logger:
                agent.invoke("hello", max_retries=3)

        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("timed out" in w.lower() for w in warning_calls)

    def test_timeout_does_not_count_as_rate_limit(self):
        """TimeoutError must not be treated as 429 — _is_rate_limit must return False."""
        from agents.base_agent import _is_rate_limit
        assert not _is_rate_limit(TimeoutError("LLM call timed out after 60s"))


# ── Token tracker ─────────────────────────────────────────────────────────────

from langchain_core.outputs import LLMResult, ChatGeneration
from unittest.mock import PropertyMock


def _make_llm_result(gen_metadatas, llm_output=None):
    """Build a minimal LLMResult with one gen_list from the given metadata dicts."""
    gens = []
    for meta in gen_metadatas:
        msg = MagicMock()
        msg.usage_metadata = meta if meta else None
        gen = MagicMock(spec=ChatGeneration)
        gen.message = msg
        gens.append(gen)
    return LLMResult(generations=[gens], llm_output=llm_output)


class TestTokenTracker:
    from utils.token_tracker import TokenTracker

    def _tracker(self):
        from utils.token_tracker import TokenTracker
        return TokenTracker("Test", input_cost_per_token=1e-6, output_cost_per_token=5e-6)

    def test_single_gen_with_metadata(self):
        t = self._tracker()
        result = _make_llm_result([{"input_tokens": 100, "output_tokens": 50}])
        t.on_llm_end(result)
        assert t.usage.input_tokens == 100
        assert t.usage.output_tokens == 50

    def test_multiple_gens_all_counted(self):
        """All generations with metadata must be summed — no early return."""
        t = self._tracker()
        result = _make_llm_result([
            {"input_tokens": 100, "output_tokens": 50},
            {"input_tokens": 200, "output_tokens": 80},
        ])
        t.on_llm_end(result)
        assert t.usage.input_tokens == 300
        assert t.usage.output_tokens == 130

    def test_openai_fallback_used_when_no_metadata(self):
        t = self._tracker()
        result = _make_llm_result(
            [None],  # no usage_metadata on the generation
            llm_output={"token_usage": {"prompt_tokens": 70, "completion_tokens": 30}},
        )
        t.on_llm_end(result)
        assert t.usage.input_tokens == 70
        assert t.usage.output_tokens == 30

    def test_openai_fallback_not_used_when_metadata_present(self):
        """If any gen has metadata, the llm_output fallback must be ignored."""
        t = self._tracker()
        result = _make_llm_result(
            [{"input_tokens": 100, "output_tokens": 50}],
            llm_output={"token_usage": {"prompt_tokens": 999, "completion_tokens": 999}},
        )
        t.on_llm_end(result)
        assert t.usage.input_tokens == 100  # NOT 999
        assert t.usage.output_tokens == 50

    def test_empty_generations_no_crash(self):
        t = self._tracker()
        result = LLMResult(generations=[[]], llm_output=None)
        t.on_llm_end(result)
        assert t.usage.input_tokens == 0
        assert t.usage.output_tokens == 0


# ── combined_report ───────────────────────────────────────────────────────────

from utils.token_tracker import combined_report


class TestCombinedReport:
    def _tracker_with_usage(self, name, input_tokens, output_tokens):
        from utils.token_tracker import TokenTracker
        t = TokenTracker(name, input_cost_per_token=1e-6, output_cost_per_token=5e-6)
        t._usage.input_tokens = input_tokens
        t._usage.output_tokens = output_tokens
        return t

    def test_contains_agent_name(self):
        t = self._tracker_with_usage("Injection Expert", 100, 50)
        report = combined_report([t])
        assert "Injection Expert" in report

    def test_contains_token_counts(self):
        t = self._tracker_with_usage("Auth Expert", 200, 80)
        report = combined_report([t])
        assert "200" in report
        assert "80" in report

    def test_totals_summed_across_agents(self):
        trackers = [
            self._tracker_with_usage("Agent A", 100, 50),
            self._tracker_with_usage("Agent B", 200, 100),
        ]
        report = combined_report(trackers)
        assert "300" in report   # total input
        assert "150" in report   # total output

    def test_empty_trackers_list(self):
        report = combined_report([])
        assert "TOTAL" in report

    def test_contains_cost_estimate(self):
        t = self._tracker_with_usage("Secrets Expert", 1000, 500)
        report = combined_report([t])
        assert "$" in report
