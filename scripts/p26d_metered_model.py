"""Persistent per-request DeepSeek budget guard for isolated evaluation."""

import json
from pathlib import Path
from time import perf_counter


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data/generated/p26d-budget-ledger.json"
MAX_REQUESTS = 120
STOP_RMB = 7.5
MAX_RMB = 8
NEXT_REQUEST_RESERVE_RMB = 0.1


def _write_ledger(rows):
    temporary = LEDGER.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    temporary.replace(LEDGER)


def read_ledger():
    return json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else []


def cost_summary(rows=None):
    rows = read_ledger() if rows is None else rows
    return {
        "requests": len(rows),
        "upper_rmb": round(sum(row.get("upper_rmb", 0) for row in rows), 6),
        "estimated_rmb": round(sum(row.get("estimated_rmb", 0) for row in rows), 6),
        "input_tokens": sum(row.get("input_tokens", 0) for row in rows),
        "output_tokens": sum(row.get("output_tokens", 0) for row in rows),
    }


class BudgetedModel:
    def __init__(self, model, limit=MAX_REQUESTS):
        self.model = model
        self.limit = min(limit, MAX_REQUESTS)
        self.calls = []

    def invoke(self, messages):
        rows = read_ledger()
        cost = cost_summary(rows)
        if (len(self.calls) >= self.limit or len(rows) >= MAX_REQUESTS or
                cost["upper_rmb"] >= STOP_RMB or
                cost["upper_rmb"] + NEXT_REQUEST_RESERVE_RMB > MAX_RMB or
                any(not row.get("usage_complete", False) for row in rows)):
            raise RuntimeError("P2.6d API budget or usage stop line reached")
        record = {"index": len(rows) + 1, "seconds": None,
                  "usage_complete": False, "error": None}
        start = perf_counter()
        try:
            reply = self.model.invoke(messages)
            meta = getattr(reply, "response_metadata", {}) or {}
            usage = meta.get("token_usage", {}) or {}
            fallback = getattr(reply, "usage_metadata", {}) or {}
            input_tokens = usage.get("prompt_tokens", fallback.get("input_tokens"))
            output_tokens = usage.get("completion_tokens", fallback.get("output_tokens"))
            if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
                record["error"] = "missing_token_usage"
                raise RuntimeError("DeepSeek token usage missing; stop further calls")
            hit = usage.get("prompt_cache_hit_tokens")
            miss = usage.get("prompt_cache_miss_tokens")
            if not isinstance(hit, int) or not isinstance(miss, int):
                hit, miss = 0, input_tokens
                record["cache_split_available"] = False
            else:
                record["cache_split_available"] = True
            details = usage.get("completion_tokens_details", {}) or {}
            record.update(input_tokens=input_tokens, output_tokens=output_tokens,
                          cache_hit_tokens=hit, cache_miss_tokens=miss,
                          reasoning_tokens=details.get("reasoning_tokens", 0),
                          upper_rmb=round((input_tokens * 2 + output_tokens * 8) / 1_000_000, 6),
                          estimated_rmb=round((hit * 0.04 + miss * 2 + output_tokens * 8) / 1_000_000, 6),
                          usage_complete=True)
            return reply
        except Exception as exc:
            record["error"] = type(exc).__name__
            raise
        finally:
            record["seconds"] = round(perf_counter() - start, 3)
            rows.append(record)
            _write_ledger(rows)
            self.calls.append(record)
