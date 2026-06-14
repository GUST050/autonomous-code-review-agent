"""
config.py — Single source of truth for all agent and system configuration.

Change model, pricing, or thresholds here. Nothing else needs updating.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelConfig:
    """Immutable configuration for one LLM model."""
    model: str
    provider: str                   # "anthropic" | "xai" | "openai"
    temperature: float
    max_tokens: int
    input_cost_per_million: float   # USD per 1M input tokens
    output_cost_per_million: float  # USD per 1M output tokens

    @property
    def input_cost_per_token(self) -> float:
        return self.input_cost_per_million / 1_000_000

    @property
    def output_cost_per_token(self) -> float:
        return self.output_cost_per_million / 1_000_000


# ── Model definitions ─────────────────────────────────────────────────────────

CLAUDE_HAIKU = ModelConfig(
    model="claude-haiku-4-5-20251001",
    provider="anthropic",
    temperature=0.1,
    max_tokens=1500,
    input_cost_per_million=0.80,
    output_cost_per_million=4.00,
)

CLAUDE_SONNET = ModelConfig(
    model="claude-sonnet-4-6",
    provider="anthropic",
    temperature=0.1,
    max_tokens=4000,
    input_cost_per_million=3.00,
    output_cost_per_million=15.00,
)

GROK_MINI = ModelConfig(
    model="grok-3-mini",
    provider="xai",
    temperature=0.1,
    max_tokens=1500,
    input_cost_per_million=0.30,
    output_cost_per_million=0.50,
)

GPT4O_MINI = ModelConfig(
    model="gpt-4o-mini",
    provider="openai",
    temperature=0.1,
    max_tokens=1500,
    input_cost_per_million=0.15,
    output_cost_per_million=0.60,
)

# ── Agent → model assignments ─────────────────────────────────────────────────
# Anthropic: precise instruction-following → critical security patterns
# xAI Grok:  logical/mathematical reasoning → algorithms and flow analysis
# OpenAI:    broad code understanding → quality and style

AGENT_CONFIGS: dict[str, ModelConfig] = {
    "Injection Expert":   CLAUDE_HAIKU,   # Anthropic — security precision
    "Auth Expert":        CLAUDE_HAIKU,   # Anthropic — security precision
    "Secrets Expert":     GROK_MINI,      # xAI — pattern recognition
    "Performance Expert": GROK_MINI,      # xAI — algorithmic reasoning
    "Code Quality Expert": GPT4O_MINI,    # OpenAI — code style and structure
    "Fix Generator":      CLAUDE_SONNET,  # Anthropic Sonnet — code generation
}

# Fast model for fix generation when max severity is below APPROVAL_THRESHOLD
FIX_GENERATOR_FAST = CLAUDE_HAIKU

# ── System settings ───────────────────────────────────────────────────────────

APPROVAL_THRESHOLD: int = 80  # Severity >= this triggers human approval
MAX_RETRIES: int = 4          # LLM call retries (includes rate-limit retries)
LLM_TIMEOUT: int = 60         # Seconds before a single LLM call is considered hung
FIX_MAX_WORKERS: int = 4      # Max parallel LLM calls in fix generation

# Shared severity scale — used in every agent system prompt
SEVERITY_SCALE = """\
Severity 0-100:
  90-100 = Critical — directly exploitable, immediate risk
  70-89  = High — significant risk under real conditions
  50-69  = Medium — exploitable with effort or limited impact
  30-49  = Low — minor risk or theoretical
  0-29   = Negligible or none found"""
