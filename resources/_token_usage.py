import json
import os
from datetime import datetime, timezone

_TOKEN_LOG_PATH = os.environ.get(
    "ZEEKER_TOKEN_LOG", "/workspace/agent/token_usage.jsonl"
)


def _log_token_usage(
    *,
    agent: str,
    endpoint: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    call_type: str = "summary",
) -> None:
    """Append a token-usage record to the shared JSONL log."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "endpoint": endpoint,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "call_type": call_type,
    }
    try:
        os.makedirs(os.path.dirname(_TOKEN_LOG_PATH), exist_ok=True)
        with open(_TOKEN_LOG_PATH, "a") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass
