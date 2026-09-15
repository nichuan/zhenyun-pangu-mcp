#!/usr/bin/env python3
"""Measure read-only knowledge paths without printing business data or credentials."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections.abc import Callable

from zhenyun_pangu_mcp.knowledge_base import service


def _measure(call: Callable[[], str], repeat: int) -> dict[str, object]:
    durations: list[float] = []
    sizes: list[int] = []
    failures = 0
    for _ in range(repeat):
        started = time.perf_counter()
        result = call()
        durations.append((time.perf_counter() - started) * 1000)
        sizes.append(len(result))
        failures += int(result.startswith("❌"))
    return {
        "median_ms": round(statistics.median(durations), 1),
        "min_ms": round(min(durations), 1),
        "max_ms": round(max(durations), 1),
        "output_chars_median": int(statistics.median(sizes)),
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="订单状态修复")
    parser.add_argument("--system", default="盘古")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1 or args.repeat > 10:
        parser.error("--repeat must be between 1 and 10")

    cases: dict[str, Callable[[], str]] = {
        "template_keyword": lambda: service.search_sql_templates(
            args.query, system=args.system, verified_only=True, limit=3, use_semantic=False,
        ),
        "template_hybrid": lambda: service.search_sql_templates(
            args.query, system=args.system, verified_only=True, limit=3, use_semantic=True,
        ),
        "search_pangu": lambda: service.search_pangu(
            args.query, system=args.system, top_k=3,
        ),
        "diagnose_context": lambda: service.diagnose_context(
            args.query, system=args.system, limit=3,
        ),
    }
    report = {
        "query_length": len(args.query),
        "repeat": args.repeat,
        "results": {name: _measure(call, args.repeat) for name, call in cases.items()},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
