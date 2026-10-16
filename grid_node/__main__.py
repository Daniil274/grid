"""Run a node: ``python -m grid_node``.

    # A browser machine the server connects to (in its container):
    python -m grid_node --driver browser --listen 0.0.0.0:7700 --token-env NODE_TOKEN

    # Your computer, paired once with the code the web chat's Devices page gives:
    python -m grid_node --server https://grid.example --pair ABCDE-FGHJK --driver browser
    # and from then on:
    python -m grid_node --server https://grid.example --driver browser
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
from pathlib import Path

from grid_node.node import Node, dial, listen


def _drivers(names, args):
    drivers = []
    for name in names:
        if name == "browser":
            from grid_node.drivers.browser import BrowserDriver

            drivers.append(BrowserDriver(Path(args.profile), headless=args.headless))
        else:
            raise SystemExit(f"unknown driver: {name}")
    return drivers


TOKEN_FILE = Path.home() / ".grid-node" / "token.json"


def _pair(server: str, code: str, name: str, kind: str) -> str:
    """Trade the pairing code for this device's token and keep it (readable by this user only)."""
    import json
    import urllib.request

    request = urllib.request.Request(
        server.rstrip("/") + "/node/pair",
        data=json.dumps({"code": code, "name": name, "kind": kind}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        paired = json.load(response)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.touch(mode=0o600, exist_ok=True)
    TOKEN_FILE.chmod(0o600)
    TOKEN_FILE.write_text(json.dumps({"server": server, "device": paired["device"], "token": paired["token"]}))
    print(f"Paired as {paired['device']}; the token is kept in {TOKEN_FILE}.")
    return paired["token"]


def _saved_token(server: str) -> str:
    import json

    try:
        saved = json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        return ""
    return saved.get("token", "") if saved.get("server", "").rstrip("/") == server.rstrip("/") else ""


def _connect_url(server: str) -> str:
    base = server.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[8:] + "/node/connect"
    if base.startswith("http://"):
        return "ws://" + base[7:] + "/node/connect"
    return base


async def _main(args) -> None:
    token = os.environ.get(args.token_env, "")
    if args.server:
        if args.pair:
            token = _pair(args.server, args.pair, args.name, args.kind)
        token = token or _saved_token(args.server)
        args.dial = _connect_url(args.server)
    if not token:
        raise SystemExit(f"Pair first (--server URL --pair CODE), or set the token in ${args.token_env}.")
    drivers = _drivers(args.driver, args)
    for driver in drivers:
        await driver.start()
    node = Node(name=args.name, kind=args.kind, drivers=drivers)
    try:
        if args.listen:
            host, _, port = args.listen.rpartition(":")
            await listen(node, host or "0.0.0.0", int(port), token)
        else:
            await dial(node, args.dial, token)
    finally:
        for driver in drivers:
            await driver.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m grid_node", description="A device's side of Grid")
    parser.add_argument("--driver", action="append", default=[], help="browser (repeatable)")
    way = parser.add_mutually_exclusive_group(required=True)
    way.add_argument("--listen", help="HOST:PORT the server connects to")
    way.add_argument("--dial", help="The server's wss://…/node/connect URL")
    way.add_argument("--server", help="The Grid web chat's address, e.g. https://grid.example")
    parser.add_argument("--pair", help="The pairing code from the Devices page (once)")
    parser.add_argument("--token-env", default="GRID_DEVICE_TOKEN", help="Environment variable holding the token")
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--kind", default="desktop", help="browser, desktop or mobile")
    parser.add_argument("--profile", default=str(Path.home() / ".grid-node" / "profile"))
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not args.driver:
        parser.error("name at least one --driver")
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
