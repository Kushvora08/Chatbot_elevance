from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import json
import os
import sys

from .config import PipelineConfig
from .models import EvaluationCase
from .orchestrator import KnowledgeBasePipeline
from .scheduler import PipelineScheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Knowledge-base update pipeline")
    parser.add_argument("--config", default=None, help="Path to pipeline JSON config")
    parser.add_argument("--source", default=None, help="Override source document directory")
    parser.add_argument("--state", default=None, help="Override state directory")
    parser.add_argument("--token", default=None, help="Access token; defaults to KB_PIPELINE_TOKEN or admin-token")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-config", help="Write a sample config")
    update = subparsers.add_parser("update", help="Process changed documents and activate if allowed")
    update.add_argument("--eval", default=None, help="JSON file with evaluation cases")
    update.add_argument("--no-activate", action="store_true", help="Create an approved version without activation")

    subparsers.add_parser("activate-pending", help="Activate the latest approved pending version")
    rollback = subparsers.add_parser("rollback", help="Roll back active version")
    rollback.add_argument("--version", default=None, help="Specific version to roll back to")

    query = subparsers.add_parser("query", help="Query active knowledge base")
    query.add_argument("question")

    subparsers.add_parser("health", help="Run a health check")
    subparsers.add_parser("schedule-once", help="Run scheduler once if update or retry is due")

    args = parser.parse_args(argv)
    if args.command == "init-config":
        path = Path(args.config or "kb_pipeline.config.json")
        PipelineConfig().write_sample(path)
        print_json({"status": "created", "path": str(path)})
        return 0

    pipeline = build_pipeline(args)
    token = args.token or os.environ.get("KB_PIPELINE_TOKEN") or "admin-token"
    if args.command == "update":
        cases = load_eval_cases(args.eval)
        result = pipeline.run_update(token=token, eval_cases=cases, activate=not args.no_activate)
        print_json(result.to_dict())
        return 0 if result.status not in {"failed", "rejected", "rolled_back"} else 1
    if args.command == "activate-pending":
        print_json(pipeline.activate_pending(token=token).to_dict())
        return 0
    if args.command == "rollback":
        print_json(pipeline.rollback(token=token, target_version_id=args.version).to_dict())
        return 0
    if args.command == "query":
        print_json(pipeline.query(args.question, token=token).to_dict())
        return 0
    if args.command == "health":
        print_json(pipeline.health_check(token=token).to_dict())
        return 0
    if args.command == "schedule-once":
        print_json(PipelineScheduler(pipeline).run_due(token=token).to_dict())
        return 0
    return 2


def build_pipeline(args: argparse.Namespace) -> KnowledgeBasePipeline:
    config = PipelineConfig.from_file(args.config) if args.config else PipelineConfig()
    if args.source:
        config.storage.source_dir = args.source
    if args.state:
        config.storage.state_dir = args.state
    return KnowledgeBasePipeline(config)


def load_eval_cases(path: str | None) -> list[EvaluationCase]:
    if not path:
        return []
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if isinstance(raw, dict):
        raw = raw.get("cases", [])
    return [EvaluationCase.from_any(item) for item in raw]


def print_json(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())

