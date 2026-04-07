"""
Cost tracker for Anthropic API calls.

Usage:
    from cost_tracker import tracked_call, log_cost

    # Option 1: wrap the call
    response = tracked_call(
        client_ai, sheet,
        script="bot.py",
        category="extraction",
        model="claude-opus-4-5",
        max_tokens=1536,
        messages=[...],
    )

    # Option 2: log manually after making the call yourself
    response = client_ai.messages.create(...)
    log_cost(sheet, script="bot.py", category="extraction", response=response)

Costs tab schema:
    Timestamp | Script | Category | Model | Input Tokens | Output Tokens | Cost USD
"""
import os
from datetime import datetime, timezone

# Claude Opus 4.5 pricing (USD per 1M tokens). Update if Anthropic changes rates.
MODEL_PRICING = {
    "claude-opus-4-5": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
}

DEFAULT_PRICING = {"input": 15.00, "output": 75.00}  # Fall back to Opus pricing if model unknown


def compute_cost(model, input_tokens, output_tokens):
    pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
    return (input_tokens * pricing["input"] / 1_000_000) + (output_tokens * pricing["output"] / 1_000_000)


def log_cost(costs_sheet, script, category, response, model=None):
    """Append a row to the Costs tab based on an Anthropic API response."""
    try:
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        actual_model = model or getattr(response, "model", "unknown")
        cost = compute_cost(actual_model, input_tokens, output_tokens)
        now = datetime.now(timezone.utc).isoformat()
        costs_sheet.append_row([
            now, script, category, actual_model,
            input_tokens, output_tokens, f"{cost:.6f}"
        ])
    except Exception as e:
        # Never let cost logging break the actual work
        print(f"    (cost logging failed: {e})")


def tracked_call(client_ai, costs_sheet, script, category, **kwargs):
    """Make an Anthropic API call and log its cost to the sheet."""
    model = kwargs.get("model", "claude-opus-4-5")
    response = client_ai.messages.create(**kwargs)
    log_cost(costs_sheet, script, category, response, model=model)
    return response
