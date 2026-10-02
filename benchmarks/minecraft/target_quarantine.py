from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from benchmarks.minecraft.run_lock import (
    MinecraftTargetLockError,
    clear_minecraft_target_quarantine,
    read_minecraft_target_lock_status,
)


DEFAULT_LOCK_ROOT = Path("result/minecraft/.locks")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lock_root = Path(args.lock_root)
    try:
        if args.command == "status":
            status = read_minecraft_target_lock_status(
                lock_root=lock_root,
                host=args.host,
                port=args.port,
            )
            payload = {
                "host": args.host,
                "port": args.port,
                **status,
            }
        else:
            payload = clear_minecraft_target_quarantine(
                lock_root=lock_root,
                host=args.host,
                port=args.port,
                reason=args.reason,
                acknowledge_target_safe=args.acknowledge_target_safe,
                force_corrupt=args.force_corrupt,
            )
    except (MinecraftTargetLockError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__}, indent=2))
        return 1
    print(json.dumps(payload, indent=2))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect or clear Minecraft target quarantine")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "clear"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--host", required=True)
        subparser.add_argument("--port", required=True, type=int)
        subparser.add_argument(
            "--lock-root",
            default=os.environ.get("VILLAGER_MINECRAFT_LOCK_ROOT", str(DEFAULT_LOCK_ROOT)),
        )
        if command == "clear":
            subparser.add_argument("--reason", required=True)
            subparser.add_argument("--acknowledge-target-safe", action="store_true")
            subparser.add_argument("--force-corrupt", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
