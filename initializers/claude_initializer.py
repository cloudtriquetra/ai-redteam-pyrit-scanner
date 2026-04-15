"""
initializers/claude_initializer.py
====================================
Registers Claude models into PyRIT's TargetRegistry using the
Anthropic OpenAI compatibility layer + PyRIT's OpenAIChatTarget.

Claude's API is OpenAI-compatible — we just point base_url at
https://api.anthropic.com/v1/ and use the Anthropic API key.

Usage in pyrit_conf or scan.py:
    --initialization-scripts initializers/claude_initializer.py
"""

import os

from pyrit.prompt_target import OpenAIChatTarget
from pyrit.common import default_values
from pyrit.models import PromptRequestPiece


ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/"
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

if not ANTHROPIC_API_KEY:
    raise EnvironmentError(
        "ANTHROPIC_API_KEY is not set. "
        "Add it to your .env file or environment variables."
    )


def get_claude_target(model: str = "claude-haiku-4-5-20251001") -> OpenAIChatTarget:
    """
    Returns a PyRIT OpenAIChatTarget configured for Claude via
    the Anthropic OpenAI compatibility endpoint.

    Args:
        model: Claude model string. Defaults to claude-haiku-4-5-20251001.
               Other options: claude-sonnet-4-6, claude-opus-4-6
    """
    return OpenAIChatTarget(
        model_name=model,
        endpoint=ANTHROPIC_BASE_URL,
        api_key=ANTHROPIC_API_KEY,
        headers={"anthropic-version": "2023-06-01"},
        max_tokens=1024,
    )


# ── Register into PyRIT TargetRegistry ────────────────────────────────────────
# PyRIT calls this module as an initialization script.
# Targets registered here become available via --target <name> in pyrit_scan.

try:
    from pyrit.common.default_values import TargetRegistry

    TargetRegistry.register(
        name="claude_haiku",
        target=get_claude_target("claude-haiku-4-5-20251001"),
    )
    TargetRegistry.register(
        name="claude_sonnet",
        target=get_claude_target("claude-sonnet-4-6"),
    )
    print("[claude_initializer] Registered: claude_haiku, claude_sonnet")

except Exception as e:
    # TargetRegistry API may differ — scan.py also calls get_claude_target() directly
    print(f"[claude_initializer] TargetRegistry not available ({e}) — using direct target mode")
