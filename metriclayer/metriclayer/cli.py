"""Command line interface: build / plan / query / validate.

Examples (run with ``python3 -I -m metriclayer ...``)::

    python3 -I -m metriclayer build   --model model.json --data data.json --db metrics.db
    python3 -I -m metriclayer plan    --model model.json --query queries/q1.json
    python3 -I -m metriclayer query   --model model.json --db metrics.db --query queries/q1.json
    python3 -I -m metriclayer validate--model model.json --data data.json
"""

from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from typing import Any

from .errors import MetricLayerError
from .model import load_model
from .compile import load_query, MISSING
from .engine import build_database
from .api import run_query, explain_query


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="metriclayer",
        description="Typed, path-explicit metric layer over SQLite",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="validate data and create the SQLite DB")
    p_build.add_argument("--model", required=True)
    p_build.add_argument("--data", required=True)
    p_build.add_argument("--db", required=True)

    p_plan = sub.add_parser("plan", help="compile a query and print its plan")
    p_plan.add_argument("--model", required=True)
    p_plan.add_argument("--query", required=True)

    p_query = sub.add_parser("query", help="compile and execute a query")
    p_query.add_argument("--model", required=True)
    p_query.add_argument("--db", required=True)
    p_query.add_argument("--query", required=True)
    p_query.add_argument("--json", action="store_true",
                         help="emit exact JSON values instead of a text table")

    p_val = sub.add_parser("validate", help="validate model + data only")
    p_val.add_argument("--model", required=True)
    p_val.add_argument("--data", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            model = load_model(args.model)
            report = build_database(model, args.data, args.db)
            print(json.dumps(report, indent=2, ensure_ascii=False))
        elif args.command == "plan":
            plan = explain_query(args.model, args.query)
            print(plan.to_json())
        elif args.command == "query":
            result = run_query(args.model, args.db, args.query)
            if args.json:
                print(result.to_json())
            else:
                print(render_text_table(result.to_dict()))
        elif args.command == "validate":
            model = load_model(args.model)
            # build_database performs all validation; use a throwaway DB
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
                report = build_database(model, args.data, tmp.name)
            print("OK — " + ", ".join(
                f"{t}={n} rows" for t, n in report["tables"].items()))
        return 0
    except MetricLayerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def render_text_table(payload: dict) -> str:
    dims = payload["dimensions"]
    rows = payload["rows"]
    metric_names: list[str] = []
    for r in rows:
        metric_names = list(r["metrics"].keys())
        break
    header = dims + metric_names
    table = [header]
    for r in rows:
        dim_cells = [
            "∅ MISSING" if isinstance(v, dict) and v.get("__missing__")
            else str(v)
            for v in r["dimensions"]
        ]
        metric_cells = [_format_value(r["metrics"].get(m)) for m in metric_names]
        table.append(dim_cells + metric_cells)
    widths = [max(len(str(row[i])) for row in table) for i in range(len(header))]
    lines = []
    for idx, row in enumerate(table):
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
        if idx == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _format_value(v: Any) -> str:
    if v is None:
        return "null"
    if v["type"] == "int":
        return v["value"]
    return f"{v['numerator']}/{v['denominator']}"


if __name__ == "__main__":
    raise SystemExit(main())
