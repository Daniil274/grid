"""Top-level Grid CLI entrypoint."""

from __future__ import annotations

import argparse

from core.config import Config
from core.evaluator import ExperimentEvaluator
from core.improvement_monitor import ImprovementMonitor
from core.config_proposer import ConfigProposer
from core.improvement_registry import ImprovementRegistry
from core.log_observer import run_observer_command


def main() -> int:
    parser = argparse.ArgumentParser(description="Grid control CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    observe_parser = subparsers.add_parser("observe", help="Scan logs and create improvement problems")
    observe_parser.add_argument("--config", default="config.yaml")
    observe_parser.add_argument("--workdir", default=None)
    observe_parser.add_argument("--since", default=None)
    observe_parser.add_argument("--min-occurrences", type=int, default=3)

    approve_parser = subparsers.add_parser("approve", help="Record a human approval via CLI")
    approve_parser.add_argument("--config", default="config.yaml")
    approve_parser.add_argument("--workdir", default=None)
    approve_parser.add_argument("--experiment-id", default=None)
    approve_parser.add_argument("--problem-id", default=None)
    approve_parser.add_argument("--reviewer", required=True)
    approve_parser.add_argument("--summary", required=True)

    propose_parser = subparsers.add_parser("propose", help="Create a config experiment from a problem")
    propose_parser.add_argument("--config", default="config.yaml")
    propose_parser.add_argument("--workdir", default=None)
    propose_parser.add_argument("--problem-id", required=True)
    propose_parser.add_argument("--created-by", default="cli")
    propose_parser.add_argument(
        "--no-evaluate",
        action="store_true",
        help="Skip benchmark evaluation even if auto_evaluate_proposed_experiments is enabled",
    )

    evaluate_parser = subparsers.add_parser("evaluate", help="Run benchmark evaluation for an experiment")
    evaluate_parser.add_argument("--config", default="config.yaml")
    evaluate_parser.add_argument("--workdir", default=None)
    evaluate_parser.add_argument("--experiment-id", required=True)

    canary_parser = subparsers.add_parser("canary", help="Run canary benchmark for an experiment")
    canary_parser.add_argument("--config", default="config.yaml")
    canary_parser.add_argument("--workdir", default=None)
    canary_parser.add_argument("--experiment-id", required=True)

    monitor_parser = subparsers.add_parser("monitor", help="Run post-promotion monitor for an experiment")
    monitor_parser.add_argument("--config", default="config.yaml")
    monitor_parser.add_argument("--workdir", default=None)
    monitor_parser.add_argument("--experiment-id", required=True)

    args = parser.parse_args()
    if args.command == "observe":
        return run_observer_command(
            config_path=args.config,
            workdir=args.workdir,
            since=args.since,
            min_occurrences=args.min_occurrences,
        )
    if args.command == "approve":
        config = Config(args.config, args.workdir)
        registry = ImprovementRegistry(config=config)
        if args.experiment_id:
            registry.record_review(
                target_id=args.experiment_id,
                review_type="final",
                decision="approved",
                reviewer=args.reviewer,
                reviewer_kind="human",
                summary=args.summary,
                metadata={"approved_via": "cli"},
            )
            registry.promote_experiment(
                experiment_id=args.experiment_id,
                promoter=args.reviewer,
                notes=args.summary,
            )
            print(f"Approved and entered canary {args.experiment_id}")
            return 0
        if args.problem_id:
            registry.record_review(
                target_id=args.problem_id,
                review_type="requirements",
                decision="approved",
                reviewer=args.reviewer,
                reviewer_kind="human",
                summary=args.summary,
                metadata={"approved_via": "cli"},
            )
            print(f"Approved requirements for {args.problem_id}")
            return 0
        parser.error("approve requires --experiment-id or --problem-id")
    if args.command == "propose":
        config = Config(args.config, args.workdir)
        registry = ImprovementRegistry(config=config)
        proposer = ConfigProposer(config=config, registry=registry)
        experiment = proposer.propose(
            args.problem_id,
            created_by=args.created_by,
            evaluate_after_create=False if args.no_evaluate else None,
        )
        print(experiment.id)
        return 0
    if args.command == "evaluate":
        config = Config(args.config, args.workdir)
        registry = ImprovementRegistry(config=config)
        evaluator = ExperimentEvaluator(config=config, registry=registry)
        experiment = evaluator.evaluate_experiment(args.experiment_id)
        print(experiment.id)
        return 0
    if args.command == "canary":
        config = Config(args.config, args.workdir)
        registry = ImprovementRegistry(config=config)
        monitor = ImprovementMonitor(config=config, registry=registry)
        experiment = monitor.run_canary(args.experiment_id)
        print(experiment.id)
        return 0
    if args.command == "monitor":
        config = Config(args.config, args.workdir)
        registry = ImprovementRegistry(config=config)
        monitor = ImprovementMonitor(config=config, registry=registry)
        experiment = monitor.monitor_experiment(args.experiment_id)
        print(experiment.id)
        return 0
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
