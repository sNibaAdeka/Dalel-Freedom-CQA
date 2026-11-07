"""OpenRouter key spend (USD) for benchmark accounting. Prints the key's cumulative usage; never prints the key.

    python scripts/bench_spend.py            # -> 0.123456
    python scripts/bench_spend.py --full     # usage, limit, remaining
Empty output (exit 0) when no OpenRouter key is configured.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from cqa import llm  # noqa: E402


def key_info() -> dict | None:
    llm.load_dotenv()
    base, key = os.environ.get("CQA_BASE_URL", ""), os.environ.get("CQA_API_KEY", "")
    if "openrouter" not in base or not key:
        return None
    r = httpx.get(base.rstrip("/") + "/key", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("data") or {}


if __name__ == "__main__":
    d = key_info()
    if d is not None:
        if "--full" in sys.argv:
            print({k: d.get(k) for k in ("usage", "limit", "limit_remaining")})
        else:
            print(f"{float(d.get('usage') or 0):.6f}")
