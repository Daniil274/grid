"""Operator CLI. Install and run outside the administrator/candidate container."""

import argparse
import json
import os
from pathlib import Path

from .controller import Controller
from .docker import DockerRuntime
from .models import Policy
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate a Grid revision in isolated containers"
    )
    parser.add_argument(
        "--state", type=Path, default=Path.home() / ".grid-control" / "experiments.db"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--repo", type=Path, required=True)
    evaluate.add_argument("--baseline", required=True)
    evaluate.add_argument("--candidate", required=True)
    evaluate.add_argument("--policy", type=Path, required=True)
    show = commands.add_parser("show")
    show.add_argument("experiment")
    cleanup = commands.add_parser(
        "cleanup",
        help="Remove a stopped controller's experiment containers and networks",
    )
    cleanup.add_argument("experiment")
    serve = commands.add_parser(
        "serve", help="Start the administrator's restricted control API"
    )
    serve.add_argument("--repo", type=Path, required=True)
    serve.add_argument("--policy", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    store = Store(args.state)
    runtime = DockerRuntime()
    try:
        if args.command == "serve":
            import uvicorn
            from .service import create_app

            policy = Policy.from_dict(
                json.loads(args.policy.read_text(encoding="utf-8"))
            )
            app = create_app(
                Controller(store, runtime),
                args.repo.resolve(),
                policy,
                os.environ.get("GRID_CONTROL_TOKEN", ""),
            )
            uvicorn.run(app, host=args.host, port=args.port)
            return
        if args.command == "evaluate":
            policy = Policy.from_dict(
                json.loads(args.policy.read_text(encoding="utf-8"))
            )
            result = Controller(store, runtime).evaluate(
                args.repo.resolve(), args.baseline, args.candidate, policy
            )
        elif args.command == "show":
            result = store.get(args.experiment)
        else:
            with store.lease(args.experiment):
                result = store.get(args.experiment)
                runtime.cleanup(args.experiment)
                if result["status"] == "running":
                    store.finish(
                        args.experiment,
                        "failed",
                        {
                            "error": "Experiment interrupted; resources cleaned by operator"
                        },
                    )
            result = store.get(args.experiment)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result["status"] != "accepted" and args.command == "evaluate":
            raise SystemExit(1)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f"grid-control: {error}\n")


if __name__ == "__main__":
    main()
