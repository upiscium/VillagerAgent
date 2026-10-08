from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from benchmarks.minecraft.run_lock import (
    MinecraftTargetPredecessorInspectionToken,
    MinecraftTargetLockError,
    acknowledge_minecraft_target_predecessor,
    clear_minecraft_target_quarantine,
    load_minecraft_target_storage_qualification,
    read_minecraft_target_predecessor_status,
    read_minecraft_target_lock_status,
)


DEFAULT_LOCK_ROOT = Path("result/minecraft/.locks")
_OBSERVATION_LIMITATIONS = (
    "History is observational; acknowledgement flags, reason, and operator identity are "
    "intent/audit input, not authentication. Acknowledgement does not extend a retained "
    "lease or authorize execution. Durability assumes a single-host persistent local "
    "filesystem honoring flock, fsync, and rename; those assumptions do not authenticate "
    "same-principal writers or prevent backup rollback."
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lock_root = Path(args.lock_root)
    exit_code = 0
    try:
        if args.command == "status":
            storage_qualification = (
                load_minecraft_target_storage_qualification(args.storage_qualification)
                if args.storage_qualification is not None
                else None
            )
            status = read_minecraft_target_lock_status(
                lock_root=lock_root,
                host=args.host,
                port=args.port,
            )
            # These are separate samples: history is observational and adds no
            # authority to, or changes the meaning of, the established fields.
            history = read_minecraft_target_predecessor_status(
                lock_root=lock_root,
                host=args.host,
                port=args.port,
                storage_qualification=storage_qualification,
            )
            payload = {
                "host": args.host,
                "port": args.port,
                **status,
                "history": history.to_dict(),
                "inspection_token": history.token.to_json() if history.token is not None else None,
                "storage_qualification": (
                    None
                    if storage_qualification is None
                    else {
                        "storage_profile_id": storage_qualification.storage_profile_id,
                        "storage_profile_version": storage_qualification.storage_profile_version,
                    }
                ),
            }
        elif args.command == "clear":
            payload = clear_minecraft_target_quarantine(
                lock_root=lock_root,
                host=args.host,
                port=args.port,
                reason=args.reason,
                acknowledge_target_safe=args.acknowledge_target_safe,
                force_corrupt=args.force_corrupt,
            )
        else:
            storage_qualification = load_minecraft_target_storage_qualification(
                args.storage_qualification
            )
            expected = MinecraftTargetPredecessorInspectionToken.from_json(args.expected_token)
            outcome = acknowledge_minecraft_target_predecessor(
                lock_root=lock_root,
                host=args.host,
                port=args.port,
                expected=expected,
                acknowledge_target_safe=args.acknowledge_target_safe,
                acknowledge_whole_prefix=args.acknowledge_whole_prefix,
                reason=args.reason,
                operator=args.operator,
                reconcile_unknown_history=args.reconcile_unknown_history,
                storage_qualification=storage_qualification,
            )
            snapshot = outcome.snapshot.to_dict()
            acknowledgement = snapshot.get("acknowledgement")
            if isinstance(acknowledgement, dict):
                # Keep the persisted acknowledgement fields intact while making
                # the exact inspected history/metadata coverage explicit in
                # the CLI output. This is a projection, not a new CAS input.
                acknowledgement["covered_history"] = {
                    "generation": acknowledgement["covered_generation"],
                    "ordinal": acknowledgement["covered_ordinal"],
                    "digest": acknowledgement["covered_digest"],
                }
                acknowledgement["covered_metadata"] = {
                    "raw_digest": acknowledgement["covered_metadata_digest"],
                    "kind": acknowledgement["covered_metadata_kind"],
                    "writer_epoch": acknowledgement["covered_writer_epoch"],
                    "revision": acknowledgement["covered_revision"],
                    "transition_nonce": acknowledgement["covered_transition_nonce"],
                }
            payload = {
                "status": outcome.status.value,
                "acknowledged": outcome.acknowledged,
                "inspection_token": expected.to_json(),
                "snapshot": snapshot,
                "error": outcome.error,
                "retained_lock": outcome.retained_lock,
            }
            if not outcome.acknowledged:
                exit_code = 1
    except (MinecraftTargetLockError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__}, indent=2))
        return 1
    print(json.dumps(payload, indent=2))
    return exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or clear Minecraft target quarantine and observe predecessor history. "
            "Existing status fields and predecessor history are separate observations, not "
            f"combined authority. {_OBSERVATION_LIMITATIONS}"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "clear", "predecessor-clear"):
        if command == "status":
            description = (
                "Report established lock status and a separate coherent predecessor-history "
                f"observation. {_OBSERVATION_LIMITATIONS}"
            )
        elif command == "clear":
            description = (
                "Clear only the existing quarantine state; this does not reconcile predecessor "
                f"history. {_OBSERVATION_LIMITATIONS}"
            )
        else:
            description = (
                "Reconcile predecessor history only with the exact token returned by status. "
                "This is separate from quarantine clear; do not replace a stale token with a "
                f"new inspection. {_OBSERVATION_LIMITATIONS}"
            )
        subparser = subparsers.add_parser(command, description=description)
        subparser.add_argument("--host", required=True)
        subparser.add_argument("--port", required=True, type=int)
        subparser.add_argument(
            "--lock-root",
            default=os.environ.get("VILLAGER_MINECRAFT_LOCK_ROOT", str(DEFAULT_LOCK_ROOT)),
        )
        if command in {"status", "predecessor-clear"}:
            subparser.add_argument(
                "--storage-qualification",
                required=command == "predecessor-clear",
                type=_absolute_path,
                help=(
                    "absolute storage-qualification receipt path; status may omit it but "
                    "then predecessor history cannot be reported CLEAN"
                ),
            )
        if command == "clear":
            subparser.add_argument("--reason", required=True)
            subparser.add_argument("--acknowledge-target-safe", action="store_true")
            subparser.add_argument("--force-corrupt", action="store_true")
        elif command == "predecessor-clear":
            subparser.add_argument(
                "--expected-token",
                required=True,
                help="exact canonical inspection_token string returned by status",
            )
            subparser.add_argument("--reason", required=True)
            subparser.add_argument("--operator", required=True)
            subparser.add_argument("--acknowledge-target-safe", action="store_true")
            subparser.add_argument("--acknowledge-whole-prefix", action="store_true")
            subparser.add_argument("--reconcile-unknown-history", action="store_true")
    return parser.parse_args(argv)


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("storage qualification receipt path must be absolute")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
