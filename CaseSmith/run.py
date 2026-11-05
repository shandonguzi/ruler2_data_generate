#!/usr/bin/env python3
"""CaseSmith pipeline entry point.

Migrated from LLM-PPOpt: reads queries from ``QA_case.csv``, assigns a random
persona to each sampled query, rewrites the query in that persona's style, runs
a multi-turn assistant/user interaction (with distractor noise), and exports a
turn-level CSV.
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_validation import validate_config_report
from src.enhanced_pipeline import EnhancedPersonaGenerationPipeline


STAGES = ['all', 'persona', 'query', 'interaction', 'distractor', 'training', 'export']


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persona-driven query rewriting pipeline (CSV in, CSV out)."
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--num", type=int, default=None,
        help="Number of queries to sample randomly (default: all rows; "
             "CSV order is kept when sampling everything)"
    )
    parser.add_argument(
        "--num-personas", type=int, default=None,
        help="Persona pool size (default: config persona_generation.num_personas)"
    )
    parser.add_argument(
        "--stage", choices=STAGES, default="all",
        help="Pipeline stage to run (default: all)"
    )
    parser.add_argument("--output-csv", default=None, help="Override paths.output_csv")
    parser.add_argument("--max-workers", type=int, default=None,
                        help="Override interaction_generation.max_workers")
    parser.add_argument("--skip-query-transfer", action="store_true",
                        help="Disable style transfer (improved_query = original)")
    parser.add_argument("--skip-distractor", action="store_true",
                        help="Disable noise injection")
    parser.add_argument("--restart", action="store_true",
                        help="Archive existing assignment state and re-sample")
    parser.add_argument("--resume", action="store_true",
                        help="Continue the previous run instead of starting a new sample")
    parser.add_argument("--fresh-personas", action="store_true",
                        help="Generate a brand-new persona pool instead of reusing stored ones")
    parser.add_argument("--strict", action="store_true",
                        help="Exit with code 2 when any query failed")
    parser.add_argument("--mock", action="store_true",
                        help="Force the mock LLM client for a zero-cost structural smoke test")
    parser.add_argument("--log-level", default=None, help="Override log level")
    return parser.parse_args()


def apply_overrides(config: dict, args: argparse.Namespace) -> dict:
    if args.output_csv:
        config.setdefault('paths', {})['output_csv'] = args.output_csv
    if args.max_workers:
        config.setdefault('interaction_generation', {})['max_workers'] = args.max_workers
    if args.skip_query_transfer:
        config.setdefault('query_generation', {}).setdefault('style_transfer', {})['enabled'] = False
    if args.skip_distractor:
        config.setdefault('distractor', {})['enabled'] = False
    if args.log_level:
        config.setdefault('paths', {}).setdefault('logs', {})['level'] = args.log_level
    return config


def print_validation_report(checks, issues) -> None:
    print("\nConfiguration checks:")
    for check in checks:
        print(f"  [{check.status:4}] {check.name}" + (f" — {check.details}" if check.details else ""))
    errors = [i for i in issues if i.level == 'error']
    if errors:
        print("\nConfiguration errors:")
        for issue in errors:
            print(f"  - {issue.path + ': ' if issue.path else ''}{issue.message}")


def main() -> int:
    args = parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1

    base_dir = config_path.parent
    os.chdir(base_dir)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}
    config = apply_overrides(config, args)

    if args.mock:
        os.environ['LLM_MOCK'] = '1'
        config.setdefault('api', {})['mock'] = True
        for env_name in ('OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY'):
            os.environ.pop(env_name, None)

    _, checks, issues = validate_config_report(config, config_path=str(config_path))
    print_validation_report(checks, issues)
    errors = [i for i in issues if i.level == 'error']
    if errors and not args.mock:
        print("\nFix the configuration errors above and re-run.", file=sys.stderr)
        return 1

    # Materialise CLI overrides so the pipeline (which loads from disk) sees them.
    merged_path = base_dir / f".config_run_{os.getpid()}.yaml"
    with open(merged_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)

    stage = 'interaction' if args.stage == 'distractor' else args.stage
    if args.stage == 'distractor':
        print("[INFO] Distractor is applied in real-time inside Stage 3; running the interaction stage.")

    pipeline = None
    try:
        pipeline = EnhancedPersonaGenerationPipeline(str(merged_path))
        results = pipeline.run(
            num=args.num,
            num_personas=args.num_personas,
            stage=stage,
            restart=args.restart,
            resume=args.resume,
            fresh_personas=args.fresh_personas,
        )
    except Exception as e:
        print(f"\nPipeline failed: {e}", file=sys.stderr)
        return 1
    finally:
        merged_path.unlink(missing_ok=True)
        if pipeline is not None:
            pipeline._cleanup_logging()

    failed = 0
    csv_summary = results.get('csv')
    if csv_summary:
        failed = csv_summary.get('failed_queries', 0)
    else:
        failed = sum(
            1 for a in results.get('assignments', [])
            if str(a.get('status', '')).startswith('failed')
        )

    if args.strict and failed:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
