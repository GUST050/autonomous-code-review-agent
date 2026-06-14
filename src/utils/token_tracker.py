from __future__ import annotations

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List

if TYPE_CHECKING:
    from config import ModelConfig


@dataclass
class AgentUsage:
    agent_name: str
    input_cost_per_token: float
    output_cost_per_token: float
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        return (
            self.input_tokens  * self.input_cost_per_token +
            self.output_tokens * self.output_cost_per_token
        )


class TokenTracker(BaseCallbackHandler):
    """
    En tracker per agent med korrekt prissättning per modell.
    Skapa med rätt in/out-kostnad per token för den modell agenten kör.
    """

    def __init__(self, agent_name: str, input_cost_per_token: float, output_cost_per_token: float):
        self._usage = AgentUsage(
            agent_name=agent_name,
            input_cost_per_token=input_cost_per_token,
            output_cost_per_token=output_cost_per_token,
        )

    @classmethod
    def from_config(cls, agent_name: str, model_config: "ModelConfig") -> "TokenTracker":
        """Create a tracker with pricing drawn directly from a ModelConfig."""
        return cls(
            agent_name=agent_name,
            input_cost_per_token=model_config.input_cost_per_token,
            output_cost_per_token=model_config.output_cost_per_token,
        )

    def on_llm_end(self, response: LLMResult, **_kwargs: Any):
        found_metadata = False
        for gen_list in response.generations:
            for gen in gen_list:
                meta = getattr(gen.message, "usage_metadata", None) if hasattr(gen, "message") else None
                if meta:
                    self._usage.input_tokens  += meta.get("input_tokens", 0)
                    self._usage.output_tokens += meta.get("output_tokens", 0)
                    found_metadata = True

        if not found_metadata:
            # Fallback: OpenAI llm_output format
            token_usage = (response.llm_output or {}).get("token_usage", {})
            if token_usage:
                self._usage.input_tokens  += token_usage.get("prompt_tokens", 0)
                self._usage.output_tokens += token_usage.get("completion_tokens", 0)

    @property
    def usage(self) -> AgentUsage:
        return self._usage


def combined_report(trackers: List[TokenTracker]) -> str:
    lines = ["\n TOKEN USAGE SUMMARY", "-" * 58]
    total_in = total_out = 0
    total_cost = 0.0

    for t in trackers:
        u = t.usage
        lines.append(
            f"  {u.agent_name:<24} in={u.input_tokens:>5}  out={u.output_tokens:>5}"
            f"  tot={u.total_tokens:>6}  ~${u.estimated_cost_usd:.5f}"
        )
        total_in   += u.input_tokens
        total_out  += u.output_tokens
        total_cost += u.estimated_cost_usd

    lines.append("-" * 58)
    lines.append(
        f"  {'TOTAL':<24} in={total_in:>5}  out={total_out:>5}"
        f"  tot={total_in + total_out:>6}  ~${total_cost:.5f}"
    )
    return "\n".join(lines)
