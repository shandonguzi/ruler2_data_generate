from __future__ import annotations

import argparse
import json
from pathlib import Path

from intent_engine.settings.config import load_config
from intent_engine.paths import PROJECT_ROOT


def cmd_discover(args):
    import os
    if args.resume_state:
        os.environ["INTENT_RESUME_STATE"] = "1"
    if args.resume_trace:
        os.environ["INTENT_RESUME_TRACE"] = args.resume_trace
    from intent_engine.atomic.atomic_discovery import run_atomic_discovery
    run_atomic_discovery(load_config(args.config), args.input, text_column=args.text_column,
                         sheet_name=args.sheet_name, limit=args.limit, workers=args.workers,
                         review=not args.no_review, stage=args.stage, source_path=args.source, iteration_rounds=args.iteration_rounds, iteration_scope=args.iteration_scope)


















def build_parser():
    p = argparse.ArgumentParser(description="Local-LLM iterative intent taxonomy engine")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config" / "config.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)

    discover = sub.add_parser("discover", help="schema-free atomic action extraction + BERTopic discovery")
    discovery_source = discover.add_mutually_exclusive_group(required=True)
    discovery_source.add_argument("--input", default=None)
    discovery_source.add_argument("--source", default=None, help="existing workflow intermediate JSON")
    discover.add_argument("--stage", choices=["all", "extract", "cluster", "review", "pairs", "domains", "iterate", "export"], default="all")
    discover.add_argument("--iteration-rounds", type=int, default=None, help="candidate taxonomy iterations (0 disables; default 3)")
    discover.add_argument("--resume-state", action="store_true", help="restore persisted iteration rounds")
    discover.add_argument("--resume-trace", default=None, help="reuse successful requests from a previous task")
    discover.add_argument("--iteration-scope", choices=["all", "unresolved_only", "noise_only"], default=None, help="from first round: all intents or unresolved memberships only")
    discover.add_argument("--text-column", default=None)
    discover.add_argument("--sheet", dest="sheet_name", default=None)
    discover.add_argument("--limit", type=int, default=None)
    discover.add_argument("--workers", type=int, default=4)
    discover.add_argument("--no-review", action="store_true", help="only extract, embed and cluster; no cluster reviews or taxonomy proposal")
    discover.set_defaults(func=cmd_discover)

    mapping = sub.add_parser('map-scenes',help='map existing atomic intents to S4 scenes')
    mapping.add_argument('--source',required=True)
    mapping.add_argument('--workers',type=int,choices=range(1,33),default=4)
    def map_command(args):
        from intent_engine.workflows.scene_mapping import run_mapping
        run_mapping(load_config(args.config),args.source,args.workers)
    mapping.set_defaults(func=map_command)
    tasks=sub.add_parser('export-atomic-tasks',help='export query and atomic task names without S4')
    tasks.add_argument('--source',required=True)
    tasks.add_argument('--workers',type=int,choices=range(1,33),default=4)
    def task_command(args):
        from intent_engine.workflows.atomic_tasks import run_atomic_tasks
        run_atomic_tasks(load_config(args.config),args.source,args.workers)
    tasks.set_defaults(func=task_command)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
