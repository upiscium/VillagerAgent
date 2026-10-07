"""Schema and fail-closed validation for the bounded K11 passive trace."""
from __future__ import annotations

import re
from typing import Any, Mapping

TRACE_SCHEMA = "minecraft-k11-hold-trace/1"
POLICY = "minecraft-k11-fixed-passive-sensing-policy/1"
REQUEST_SCHEMA = "minecraft-k11-visible-block-snapshot/1"
TRACE_EVENTS = frozenset({
    "window_opened", "window_closed", "due", "request", "capture",
    "response", "verified", "ingest", "qc",
})
REQUEST_KEYS = frozenset({
    "schema", "run_id", "window_id", "actor_id", "tick_index", "nonce",
    "sensor_id", "sensor_digest", "profile_digest", "ingestion_digest",
    "geometry_id", "geometry_digest", "request_hmac",
})
REQUEST_REQUIRED_KEYS = REQUEST_KEYS - {"request_hmac"}
MAX_TRACE_EVENTS = 65_536
CADENCE_NS = 1_000_000_000
DEADLINE_NS = 500_000_000
TRACE_EVENTS_PER_ACTOR_TICK = 9
MAX_FATAL_COMMITTED_CELLS = 75

_FORBIDDEN_TRACE_KEYS = frozenset({
    "action", "target", "action_target", "outcome", "delta_label", "delta_ms",
    "payload", "sensor_payload", "request_payload", "nonce", "request_hmac",
    "hmac", "hmac_sha256", "secret", "secret_key", "password", "credential",
    "credentials", "token", "api_key", "private_key",
})
_FATAL_RECEIPT_KEYS = frozenset({
    "status", "actor_id", "tick_index", "capture_seq", "committed_cells",
    "failing_cell_index", "failing_coordinate", "phase", "orphan_provenance_id",
    "orphan_provenance",
})
_FATAL_CELL_KEYS = frozenset({
    "cell_index", "coordinate", "root_id", "provenance_id", "polarity",
    "supersedes", "ingest_sequence",
})
_FATAL_RAW_COMMON_KEYS = frozenset({"cell_index", "coordinate", "state"})
_FATAL_RAW_UNKNOWN_KEYS = _FATAL_RAW_COMMON_KEYS | {"unknown_reason"}
_FATAL_RAW_KNOWN_KEYS = _FATAL_RAW_COMMON_KEYS | {"block_name", "registry_id"}
_FATAL_RAW_RECEIPT_KEYS = frozenset({"raw_observations", "raw_cell_count"})
_FATAL_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_FATAL_PHASE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_BLOCK_RE = re.compile(r"[a-z0-9_]{1,128}\Z")
_ERROR_TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,127}\Z")
_STATES = frozenset({"known_air", "known_non_air", "unknown"})
_DISPOSITIONS = frozenset({"committed", "semantic_noop", "unknown"})
_AIR_NAMES = frozenset({"air", "cave_air", "void_air"})
_UNKNOWN_REASONS = frozenset({
    "unloaded_target", "unloaded_path", "occluded", "outside_region",
    "invalid_pose", "step_limit", "incoherent_capture", "unmapped_registry",
})


def _valid_identifier(value: Any) -> bool:
    return (isinstance(value, str) and _FATAL_IDENTIFIER_RE.fullmatch(value) is not None
            and re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", value) is None)


def _valid_coordinate(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 3
            and all(type(part) is int and abs(part) <= 2**31 - 1 for part in value))


def _validate_fatal_raw_observations(value: Any, raw_cell_count: Any) -> list[str]:
    errors: list[str] = []
    if type(raw_cell_count) is not int or raw_cell_count != MAX_FATAL_COMMITTED_CELLS:
        errors.append("fatal raw_cell_count is not exactly 75")
    if not isinstance(value, list) or len(value) != MAX_FATAL_COMMITTED_CELLS:
        return errors + ["fatal raw_observations must contain exactly 75 cells"]
    for expected, observation in enumerate(value):
        prefix = f"fatal raw observation {expected}"
        if not isinstance(observation, Mapping):
            errors.append(f"{prefix} is malformed")
            continue
        fields = set(observation)
        index = observation.get("cell_index")
        if type(index) is not int or index != expected:
            errors.append(f"{prefix} cell_index is not in canonical 0..74 order")
        if not _valid_coordinate(observation.get("coordinate")):
            errors.append(f"{prefix} coordinate is invalid")
        state = observation.get("state")
        if not isinstance(state, str) or state not in _STATES:
            errors.append(f"{prefix} state is invalid")
            continue
        if state == "unknown":
            if fields != _FATAL_RAW_UNKNOWN_KEYS:
                errors.append(f"{prefix} unknown raw field set is invalid")
            reason = observation.get("unknown_reason")
            if not isinstance(reason, str) or reason not in _UNKNOWN_REASONS:
                errors.append(f"{prefix} unknown_reason is invalid")
            continue
        if fields != _FATAL_RAW_KNOWN_KEYS:
            errors.append(f"{prefix} known raw field set is invalid")
        name = observation.get("block_name")
        if not isinstance(name, str) or _BLOCK_RE.fullmatch(name) is None:
            errors.append(f"{prefix} block_name is invalid")
        elif (state == "known_air") != (name in _AIR_NAMES):
            errors.append(f"{prefix} state differs from block_name")
        registry_id = observation.get("registry_id")
        if type(registry_id) is not int or not 0 <= registry_id < 2**63:
            errors.append(f"{prefix} registry_id is invalid")
    return errors


def _validate_observations(value: Any) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    counts = {"raw_cell_count": 0, "semantic_noop_count": 0,
              "transition_count": 0, "unknown_count": 0}
    if not isinstance(value, list) or len(value) > MAX_FATAL_COMMITTED_CELLS:
        return ["observation ledger is not a bounded list"], counts
    counts["raw_cell_count"] = len(value)
    required = {"cell_index", "coordinate", "state", "disposition"}
    seen: set[int] = set()
    ordered: list[int] = []
    for index, observation in enumerate(value):
        prefix = f"observation {index}"
        if not isinstance(observation, Mapping):
            errors.append(f"{prefix} is malformed")
            continue
        fields = set(observation)
        if not required.issubset(fields):
            errors.append(f"{prefix} fields are incomplete")
            continue
        cell_index = observation.get("cell_index")
        if (type(cell_index) is not int or not 0 <= cell_index < MAX_FATAL_COMMITTED_CELLS
                or cell_index in seen):
            errors.append(f"{prefix} cell_index is invalid")
        else:
            seen.add(cell_index)
            ordered.append(cell_index)
        if not _valid_coordinate(observation.get("coordinate")):
            errors.append(f"{prefix} coordinate is invalid")
        state, disposition = observation.get("state"), observation.get("disposition")
        if not isinstance(state, str) or state not in _STATES:
            errors.append(f"{prefix} state is invalid")
            continue
        if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
            errors.append(f"{prefix} disposition is invalid")
            continue
        if disposition == "semantic_noop":
            counts["semantic_noop_count"] += 1
        elif disposition == "committed":
            counts["transition_count"] += 1
        else:
            counts["unknown_count"] += 1
        if state == "unknown":
            allowed = required | {"unknown_reason"}
            if disposition != "unknown":
                errors.append(f"{prefix} unknown state has a known disposition")
            if not fields.issubset(allowed):
                errors.append(f"{prefix} unknown observation exposes identity or extra fields")
            if "unknown_reason" in observation and (
                    not isinstance(observation.get("unknown_reason"), str)
                    or observation.get("unknown_reason") not in _UNKNOWN_REASONS):
                errors.append(f"{prefix} unknown_reason is invalid")
            continue
        expected = required | {"block_name", "registry_id"}
        if frozenset(fields) not in {frozenset(expected), frozenset(expected | {"root_id"})}:
            errors.append(f"{prefix} known observation fields are malformed")
        block_name = observation.get("block_name")
        if not isinstance(block_name, str) or _BLOCK_RE.fullmatch(block_name) is None:
            errors.append(f"{prefix} block_name is invalid")
        elif (state == "known_air") != (block_name in _AIR_NAMES):
            errors.append(f"{prefix} state differs from block_name")
        registry_id = observation.get("registry_id")
        if type(registry_id) is not int or not 0 <= registry_id < 2**63:
            errors.append(f"{prefix} registry_id is invalid")
        if disposition not in {"committed", "semantic_noop"}:
            errors.append(f"{prefix} known state has an unknown disposition")
        if disposition == "semantic_noop" and "root_id" in observation:
            errors.append(f"{prefix} semantic no-op fabricates a root")
        if "root_id" in observation and not _valid_identifier(observation.get("root_id")):
            errors.append(f"{prefix} root_id is invalid")
    if ordered != sorted(seen):
        errors.append("observations are not in capture order")
    return errors, counts


def _validate_fatal_receipt(receipt: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(receipt, Mapping):
        return ["fatal receipt fields are malformed"]
    fields = set(receipt)
    has_raw = _FATAL_RAW_RECEIPT_KEYS.issubset(fields)
    if fields != _FATAL_RECEIPT_KEYS and fields != _FATAL_RECEIPT_KEYS | _FATAL_RAW_RECEIPT_KEYS:
        return ["fatal receipt fields are malformed"]
    status, phase = receipt.get("status"), receipt.get("phase")
    if not isinstance(status, str) or status not in {"partial_commit_fatal", "sensor_ingest_fatal"}:
        errors.append("fatal receipt status is invalid")
    if not _valid_identifier(receipt.get("actor_id")):
        errors.append("fatal receipt actor_id is invalid")
    tick, capture = receipt.get("tick_index"), receipt.get("capture_seq")
    if type(tick) is not int or not 0 <= tick < 2**63:
        errors.append("fatal receipt tick_index is invalid")
    if type(capture) is not int or not 1 <= capture < 2**63:
        errors.append("fatal receipt capture_seq is invalid")
    phase_valid = isinstance(phase, str) and _FATAL_PHASE_RE.fullmatch(phase) is not None
    if not phase_valid:
        errors.append("fatal receipt phase is invalid")
    cells = receipt.get("committed_cells")
    if not isinstance(cells, list) or len(cells) > MAX_FATAL_COMMITTED_CELLS:
        errors.append("fatal receipt committed_cells is invalid")
        cells = []
    seen: set[int] = set()
    ordered: list[int] = []
    coordinates: dict[int, list[int]] = {}
    for index, cell in enumerate(cells):
        if not isinstance(cell, Mapping) or set(cell) != _FATAL_CELL_KEYS:
            errors.append(f"fatal receipt committed cell {index} is malformed")
            continue
        cell_index = cell.get("cell_index")
        valid_index = (type(cell_index) is int and 0 <= cell_index < MAX_FATAL_COMMITTED_CELLS
                       and cell_index not in seen)
        if not valid_index:
            errors.append(f"fatal receipt committed cell {index} index is invalid")
        else:
            seen.add(cell_index)
            ordered.append(cell_index)
        coordinate = cell.get("coordinate")
        if not _valid_coordinate(coordinate):
            errors.append(f"fatal receipt committed cell {index} coordinate is invalid")
        elif valid_index:
            coordinates[cell_index] = coordinate
        for name in ("root_id", "provenance_id"):
            if not _valid_identifier(cell.get(name)):
                errors.append(f"fatal receipt committed cell {index} {name} is invalid")
        if type(cell.get("polarity")) is not bool:
            errors.append(f"fatal receipt committed cell {index} polarity is invalid")
        supersedes = cell.get("supersedes")
        if (not isinstance(supersedes, list) or len(supersedes) > MAX_FATAL_COMMITTED_CELLS
                or any(not _valid_identifier(value) for value in supersedes)):
            errors.append(f"fatal receipt committed cell {index} supersedes is invalid")
        sequence = cell.get("ingest_sequence")
        if type(sequence) is not int or not 1 <= sequence < 2**63:
            errors.append(f"fatal receipt committed cell {index} ingest_sequence is invalid")
    if ordered != sorted(ordered):
        errors.append("fatal receipt committed cells are not in capture order")
    if status != ("partial_commit_fatal" if cells else "sensor_ingest_fatal"):
        errors.append("fatal receipt status contradicts committed inventory")

    failing = receipt.get("failing_cell_index")
    if failing is not None and (
        type(failing) is not int or not 0 <= failing < MAX_FATAL_COMMITTED_CELLS
        or (failing in seen and (not phase_valid or phase not in {
            "ingest_record", "runtime_bookkeeping", "persist_audit", "commit_ledger",
        }))
    ):
        errors.append("fatal receipt failing_cell_index is invalid")
    failing_coordinate = receipt.get("failing_coordinate")
    if failing_coordinate is not None and not _valid_coordinate(failing_coordinate):
        errors.append("fatal receipt failing_coordinate is invalid")
    if (failing is None) != (failing_coordinate is None):
        errors.append("fatal receipt failing cell and coordinate are unpaired")
    if (phase_valid and phase in {"runtime_bookkeeping", "persist_audit", "commit_ledger"}
            and (type(failing) is not int or failing not in seen)):
        errors.append("post-insertion fatal receipt omits the failed cell root")
    if (type(failing) is int and failing in seen and phase_valid
            and phase in {"ingest_record", "runtime_bookkeeping", "persist_audit", "commit_ledger"}
            and failing_coordinate != coordinates.get(failing)):
        errors.append("fatal receipt failing coordinate differs from committed cell")
    orphan = receipt.get("orphan_provenance_id")
    if orphan is not None and not _valid_identifier(orphan):
        errors.append("fatal receipt orphan provenance ID is invalid")
    orphan_flag = receipt.get("orphan_provenance")
    if type(orphan_flag) is not bool or (orphan is not None and orphan_flag is not True):
        errors.append("fatal receipt orphan provenance flag is invalid")
    if phase_valid and phase == "capacity_exhausted" and (
        cells or failing is not None or failing_coordinate is not None
        or orphan is not None or orphan_flag is not False
    ):
        errors.append("capacity_exhausted fatal receipt does not declare zero tick mutations")
    if has_raw:
        errors.extend(_validate_fatal_raw_observations(
            receipt.get("raw_observations"), receipt.get("raw_cell_count"),
        ))
        raw_rows = receipt.get("raw_observations")
        if isinstance(raw_rows, list) and len(raw_rows) == MAX_FATAL_COMMITTED_CELLS:
            for cell in cells:
                if not isinstance(cell, Mapping):
                    continue
                cell_index = cell.get("cell_index")
                if (type(cell_index) is not int
                        or not 0 <= cell_index < MAX_FATAL_COMMITTED_CELLS):
                    continue
                observation = raw_rows[cell_index]
                if not isinstance(observation, Mapping):
                    continue
                if (observation.get("state") == "unknown"
                        or observation.get("coordinate") != cell.get("coordinate")
                        or cell.get("polarity") != (observation.get("state") == "known_non_air")):
                    errors.append("fatal committed inventory differs from raw observation")
    return errors


def validate_request_payload(request: Mapping[str, Any]) -> bool:
    """Only fixed passive identity fields may cross the adapter boundary."""
    if not isinstance(request, Mapping):
        return False
    keys = frozenset(request)
    if not REQUEST_REQUIRED_KEYS.issubset(keys) or not keys.issubset(REQUEST_KEYS):
        return False
    if request.get("schema") != REQUEST_SCHEMA:
        return False
    for name in REQUEST_REQUIRED_KEYS - {"tick_index"}:
        if not isinstance(request.get(name), str) or not request[name]:
            return False
    tick = request.get("tick_index")
    if type(tick) is not int or tick < 0:
        return False
    if "request_hmac" in request and (not isinstance(request["request_hmac"], str)
                                      or not request["request_hmac"]):
        return False
    return True


def _forbidden_nested_fields(value: Any, *, max_items: int = 16_384,
                             max_depth: int = 32) -> list[str]:
    """Inspect nested event data with bounded traversal and cycle protection."""
    errors: list[str] = []
    seen: set[int] = set()
    count = 0

    def visit(current: Any, depth: int, path: str) -> None:
        nonlocal count
        count += 1
        if count > max_items or depth > max_depth:
            if not errors or errors[-1] != "event details exceed recursive inspection bound":
                errors.append("event details exceed recursive inspection bound")
            return
        if isinstance(current, Mapping):
            ident = id(current)
            if ident in seen:
                errors.append("event details contain recursive data")
                return
            seen.add(ident)
            try:
                for key, child in current.items():
                    if not isinstance(key, str):
                        errors.append("event detail key is not a string")
                        continue
                    if key.lower() in _FORBIDDEN_TRACE_KEYS:
                        errors.append(f"event contains forbidden semantic field {key}")
                    visit(child, depth + 1, f"{path}.{key}")
            finally:
                seen.remove(ident)
        elif isinstance(current, (list, tuple)):
            ident = id(current)
            if ident in seen:
                errors.append("event details contain recursive data")
                return
            seen.add(ident)
            try:
                for index, child in enumerate(current):
                    visit(child, depth + 1, f"{path}[{index}]")
            finally:
                seen.remove(ident)

    visit(value, 0, "event")
    return errors


def validate_hold_trace(artifact: Any) -> dict[str, Any]:
    """Validate bounded structure and scheduler linearization, not live service use."""
    errors: list[str] = []
    if not isinstance(artifact, Mapping):
        return {"valid": False, "errors": ["trace must be an object"]}
    if artifact.get("schema") != TRACE_SCHEMA:
        errors.append("trace schema mismatch")
    if artifact.get("policy") != POLICY:
        errors.append("trace policy mismatch")
    if artifact.get("cadence_ns") != CADENCE_NS:
        errors.append("trace cadence mismatch")
    if artifact.get("deadline_ns") != DEADLINE_NS:
        errors.append("trace deadline mismatch")
    t0, close = artifact.get("t0_monotonic_ns"), artifact.get("window_close_monotonic_ns")
    bounds_valid = (type(t0) is int and t0 >= 0 and type(close) is int and close > t0)
    if not bounds_valid:
        errors.append("trace window bounds are invalid")
    actors = artifact.get("actor_ids")
    if (not isinstance(actors, list) or len(actors) != 2
            or any(not isinstance(actor, str) or not actor for actor in actors)
            or (len(actors) == 2 and actors[0] == actors[1])):
        errors.append("trace must declare exactly two distinct actors")
        actors = []
    events = artifact.get("events")
    if not isinstance(events, list):
        errors.append("trace events must be a list")
        events = []
    if len(events) > MAX_TRACE_EVENTS:
        errors.append("trace exceeds maximum event bound")
    retention = artifact.get("trace_retention")
    if not isinstance(retention, Mapping):
        errors.append("trace retention metadata is missing")
    else:
        if bounds_valid:
            ticks = (close - t0 + CADENCE_NS - 1) // CADENCE_NS
            if retention.get("capacity") != TRACE_EVENTS_PER_ACTOR_TICK * ticks * 2 + 2:
                errors.append("trace capacity does not match bounded schedule size")
        capacity, retained, dropped = (retention.get("capacity"), retention.get("retained"),
                                       retention.get("dropped_count"))
        if type(capacity) is not int or not 1 <= capacity <= MAX_TRACE_EVENTS:
            errors.append("trace capacity is invalid")
        if type(retained) is not int or retained != len(events):
            errors.append("trace retained count mismatch")
        if type(capacity) is int and len(events) > capacity:
            errors.append("trace exceeds its declared capacity")
        if retention.get("truncated") is not False:
            errors.append("trace is truncated or retention status is malformed")
        if type(dropped) is not int or dropped != 0:
            errors.append("trace has dropped diagnostics")

    for expected, event in enumerate(events):
        if not isinstance(event, Mapping):
            errors.append(f"event {expected} is malformed")
            continue
        sequence, counter = event.get("sequence"), event.get("linearization_counter")
        if type(sequence) is not int or sequence != expected:
            errors.append(f"event {expected} sequence mismatch")
        if type(counter) is not int or counter != expected + 1:
            errors.append(f"event {expected} linearization counter mismatch")
        kind = event.get("event")
        kind_valid = isinstance(kind, str) and kind in TRACE_EVENTS
        if not kind_valid:
            errors.append(f"event {expected} has an unknown kind")
        timestamp = event.get("controller_monotonic_ns")
        timestamp_valid = type(timestamp) is int and timestamp >= 0
        if not timestamp_valid:
            errors.append(f"event {expected} controller timestamp is invalid")
        status = event.get("status")
        if not isinstance(status, str) or not status:
            errors.append(f"event {expected} status is invalid")
        reason = event.get("reason")
        if reason is not None and not isinstance(reason, str):
            errors.append(f"event {expected} reason is invalid")
        actor = event.get("actor_id")
        if actor is not None and (not isinstance(actor, str) or actor not in actors):
            errors.append(f"event {expected} has an unknown actor")
        tick = event.get("tick_index")
        if tick is not None and (type(tick) is not int or tick < 0):
            errors.append(f"event {expected} tick index is invalid")
        due = event.get("due_monotonic_ns")
        if kind_valid and kind in {"due", "request", "capture", "response", "verified", "ingest", "qc"}:
            if (not bounds_valid or type(tick) is not int or type(due) is not int
                    or due != t0 + tick * CADENCE_NS or due >= close):
                errors.append(f"event {expected} schedule binding is invalid")
        if kind_valid and kind == "request" and bounds_valid and timestamp_valid and type(due) is int:
            deadline = event.get("deadline_monotonic_ns")
            if (type(deadline) is not int or deadline != due + DEADLINE_NS
                    or timestamp < due or timestamp >= deadline):
                errors.append(f"event {expected} request deadline is invalid")
        if (kind_valid and kind == "ingest" and event.get("status") == "admitted"
                and bounds_valid and timestamp_valid and type(due) is int
                and timestamp >= min(close, due + DEADLINE_NS)):
            errors.append(f"event {expected} ingest was admitted after cutoff")
        errors.extend(f"event {expected}: {error}" for error in _forbidden_nested_fields(event))
        bridge = event.get("bridge_monotonic_ns_diagnostic")
        if bridge is not None and (type(bridge) is not int or bridge < 0):
            errors.append(f"event {expected} bridge diagnostic timestamp is invalid")

        count_fields = {"raw_cell_count", "semantic_noop_count", "transition_count", "unknown_count"}
        if "observations" in event:
            if not kind_valid or kind != "qc":
                errors.append(f"event {expected} observation ledger is not bound to QC")
            capture_seq = event.get("capture_seq")
            if type(capture_seq) is not int or not 1 <= capture_seq < 2**63:
                errors.append(f"event {expected} observation capture_seq is invalid")
            obs_errors, counts = _validate_observations(event.get("observations"))
            errors.extend(f"event {expected}: {error}" for error in obs_errors)
            if not count_fields.issubset(event):
                errors.append(f"event {expected} observation counts are incomplete")
            for name, value in counts.items():
                if type(event.get(name)) is not int or event.get(name) != value:
                    errors.append(f"event {expected} {name} differs from observation ledger")
            observations = event.get("observations")
            commits = ([item for item in observations if isinstance(item, Mapping)
                        and item.get("disposition") == "committed"]
                       if isinstance(observations, list) else [])
            event_status = event.get("status")
            has_inventory = (event.get("commit_marker") is True
                             or (isinstance(event_status, str) and event_status in {
                                 "partial_commit_fatal", "sensor_ingest_fatal"})
                              or "secondary_fatal_receipt" in event or "committed_cells" in event
                              or "secondary_committed_cells" in event)
            if has_inventory:
                inventory = event.get("committed_cells", event.get("secondary_committed_cells", []))
                if not isinstance(inventory, list) or len(inventory) != len(commits):
                    errors.append(f"event {expected} observation transitions differ from cell inventory")
                else:
                    valid_inventory: dict[int, Mapping[str, Any]] = {}
                    for cell in inventory:
                        if not isinstance(cell, Mapping):
                            errors.append(f"event {expected} committed cell is malformed")
                            continue
                        index = cell.get("cell_index")
                        if type(index) is not int or not 0 <= index < MAX_FATAL_COMMITTED_CELLS:
                            errors.append(f"event {expected} committed cell index is invalid")
                        elif index in valid_inventory:
                            errors.append(f"event {expected} committed cell index is duplicated")
                        else:
                            valid_inventory[index] = cell
                    for observation in commits:
                        cell_index = observation.get("cell_index")
                        cell = valid_inventory.get(cell_index) if type(cell_index) is int else None
                        if (cell is None or cell.get("coordinate") != observation.get("coordinate")
                                or cell.get("polarity") != (observation.get("state") == "known_non_air")
                                or ("root_id" in observation and cell.get("root_id") != observation.get("root_id"))):
                            errors.append(f"event {expected} observation transition differs from committed cell")
                            break
            elif commits:
                errors.append(f"event {expected} has transitions without committed inventory")
            if event.get("commit_marker") is True and (
                    event_status != "accepted" or not isinstance(event.get("committed_cells"), list)
                    or not event.get("committed_cells")):
                errors.append(f"event {expected} commit marker lacks a nonempty committed inventory")
        elif (count_fields.intersection(event)
              - ({"raw_cell_count"} if "raw_observations" in event else set())):
            errors.append(f"event {expected} has counts without an observation ledger")

    if sum(isinstance(row, Mapping) and row.get("event") == "window_opened" for row in events) != 1:
        errors.append("trace must contain exactly one window_opened event")
    if sum(isinstance(row, Mapping) and row.get("event") == "window_closed" for row in events) > 1:
        errors.append("trace contains duplicate window_closed events")

    fatal_events = [row for row in events if isinstance(row, Mapping) and row.get("event") == "qc"
                    and isinstance(row.get("status"), str)
                    and row.get("status") in {"partial_commit_fatal", "sensor_ingest_fatal"}]
    commit_markers = [row for row in events if isinstance(row, Mapping) and row.get("event") == "qc"
                      and row.get("commit_marker") is True]
    secondary = [row for row in events if isinstance(row, Mapping) and "secondary_fatal_receipt" in row]
    secondary_commits = [row for row in events if isinstance(row, Mapping)
                         and "secondary_committed_cells" in row]
    if len(secondary_commits) > 2:
        errors.append("trace exceeds the two-actor secondary inventory bound")
    for index, marker in enumerate(secondary_commits):
        cells = marker.get("secondary_committed_cells")
        capture_seq = marker.get("capture_seq", marker.get("secondary_capture_seq"))
        pseudo = {
            "status": "partial_commit_fatal" if isinstance(cells, list) and cells else "sensor_ingest_fatal",
            "actor_id": marker.get("actor_id"), "tick_index": marker.get("tick_index"),
            "capture_seq": capture_seq, "committed_cells": cells,
            "failing_cell_index": None, "failing_coordinate": None,
            "phase": "secondary_inventory", "orphan_provenance_id": None,
            "orphan_provenance": False,
        }
        errors.extend(f"secondary commit {index}: {error}" for error in _validate_fatal_receipt(pseudo))
        if (marker.get("event") != "qc" or marker.get("status") != "diagnostic_only"
                or marker.get("reason") not in ("receipt_after_unverified_fatal",
                                               "fatal_receipt_after_unverified_fatal")
                or artifact.get("fatal_status") != "sensor_ingest_unverified_fatal"):
            errors.append(f"secondary commit {index} lacks an unverified fatal diagnostic boundary")
        prior = [row for row in events if isinstance(row, Mapping)
                 and row.get("event") == "ingest" and row.get("status") == "admitted"
                 and row.get("actor_id") == marker.get("actor_id")
                 and row.get("tick_index") == marker.get("tick_index")]
        if (len(prior) != 1 or type(marker.get("sequence")) is not int
                or (len(prior) == 1 and (type(prior[0].get("sequence")) is not int
                                        or marker["sequence"] <= prior[0]["sequence"]))):
            errors.append(f"secondary commit {index} lacks a prior capture reservation")
        if marker.get("secondary_commit_status") == "committed":
            sequences = ([cell.get("ingest_sequence") for cell in cells]
                         if isinstance(cells, list) and all(isinstance(cell, Mapping) for cell in cells)
                         else None)
            if marker.get("secondary_eac_ingest_sequences") != sequences:
                errors.append(f"secondary commit {index} EAC ingest sequence mismatch")
    if secondary and not fatal_events:
        errors.append("secondary fatal inventory has no primary fatal QC event")
    if len(secondary) > 1:
        errors.append("trace exceeds the bounded secondary fatal inventory count")
    for marker_index, marker in enumerate(commit_markers):
        cells, actor, tick = marker.get("committed_cells"), marker.get("actor_id"), marker.get("tick_index")
        pseudo = {
            "status": "partial_commit_fatal" if isinstance(cells, list) and cells else "sensor_ingest_fatal",
            "actor_id": actor, "tick_index": tick, "capture_seq": marker.get("capture_seq"),
            "committed_cells": cells, "failing_cell_index": None,
            "failing_coordinate": None, "phase": "commit_marker",
            "orphan_provenance_id": None, "orphan_provenance": False,
        }
        errors.extend(f"commit marker {marker_index}: {error}" for error in _validate_fatal_receipt(pseudo))
        sequences = ([cell.get("ingest_sequence") for cell in cells]
                     if isinstance(cells, list) and all(isinstance(cell, Mapping) for cell in cells)
                     else None)
        if marker.get("eac_ingest_sequences") != sequences:
            errors.append(f"commit marker {marker_index} EAC ingest sequences mismatch")
        if (marker.get("status") != "accepted" or marker.get("commit_status") != "committed"
                or marker.get("commit_actor_id") != actor or marker.get("commit_tick_index") != tick):
            errors.append(f"commit marker {marker_index} status is invalid")
        admitted = [row for row in events if isinstance(row, Mapping)
                    and row.get("event") == "ingest" and row.get("status") == "admitted"
                    and row.get("actor_id") == actor and row.get("tick_index") == tick]
        if len(admitted) != 1:
            errors.append(f"commit marker {marker_index} lacks one ingest reservation")
        elif (type(marker.get("sequence")) is not int or type(admitted[0].get("sequence")) is not int
              or marker["sequence"] <= admitted[0]["sequence"]
              or type(marker.get("controller_monotonic_ns")) is not int
              or type(admitted[0].get("controller_monotonic_ns")) is not int
              or marker["controller_monotonic_ns"] < admitted[0]["controller_monotonic_ns"]):
            errors.append(f"commit marker {marker_index} precedes its ingest reservation")
        due, marker_ns = marker.get("due_monotonic_ns"), marker.get("controller_monotonic_ns")
        if bounds_valid and type(due) is int and type(marker_ns) is int and marker_ns >= min(close, due + DEADLINE_NS):
            errors.append(f"commit marker {marker_index} occurs after its cutoff")
        if not isinstance(actor, str) or actor not in actors:
            errors.append(f"commit marker {marker_index} actor is invalid")

    marker_fields = {"fatal_status", "fatal_reason", "fatal_receipt", "scientifically_eligible", "censored"}
    marker_present = bool(marker_fields.intersection(artifact))
    unverified = artifact.get("unverified_ingest_failure")
    unverified_events = [row for row in events if isinstance(row, Mapping) and row.get("event") == "qc"
                         and row.get("status") == "sensor_ingest_unverified_fatal"]
    if marker_present or fatal_events or secondary or unverified_events:
        if not marker_fields.issubset(artifact):
            errors.append("fatal eligibility marker fields are incomplete")
        receipt = artifact.get("fatal_receipt")
        fatal_status = artifact.get("fatal_status")
        if fatal_status == "sensor_ingest_unverified_fatal":
            if receipt is not None or fatal_events or secondary:
                errors.append("unverified ingest fatal must not fabricate a receipt")
            if artifact.get("fatal_reason") != "sensor_ingest_unverified_fatal":
                errors.append("unverified ingest fatal reason is invalid")
            if artifact.get("scientifically_eligible") is not False or artifact.get("censored") is not True:
                errors.append("unverified ingest fatal eligibility marker is invalid")
            failure_keys = {"actor_id", "tick_index", "mutation_status", "error_type"}
            if not isinstance(unverified, Mapping) or set(unverified) != failure_keys:
                errors.append("unverified ingest failure marker is malformed")
            else:
                actor, tick = unverified.get("actor_id"), unverified.get("tick_index")
                if not isinstance(actor, str) or actor not in actors:
                    errors.append("unverified ingest failure actor is invalid")
                if type(tick) is not int or tick < 0:
                    errors.append("unverified ingest failure tick is invalid")
                if unverified.get("mutation_status") != "unknown":
                    errors.append("unverified ingest mutation status must remain unknown")
                if (not isinstance(unverified.get("error_type"), str)
                        or _ERROR_TYPE_RE.fullmatch(unverified["error_type"]) is None):
                    errors.append("unverified ingest error type is invalid")
            if len(unverified_events) != 1:
                errors.append("unverified ingest fatal must have exactly one QC event")
            else:
                event = unverified_events[0]
                if (event.get("reason") != "sensor_ingest_unverified_fatal"
                        or event.get("mutation_status") != "unknown"
                        or event.get("scientifically_eligible") is not False
                        or event.get("censored") is not True):
                    errors.append("unverified ingest fatal QC marker is invalid")
                forbidden_fabrications = {
                    "capture_seq", "committed_cells", "fatal_receipt", "failing_cell_index",
                    "failing_coordinate", "tick_mutations", "orphan_provenance_id",
                    "raw_observations", "raw_cell_count",
                }
                if forbidden_fabrications & set(event):
                    errors.append("unverified ingest fatal fabricates mutation inventory")
                matching = [row for row in events if isinstance(row, Mapping)
                            and row.get("event") == "ingest" and row.get("status") == "admitted"
                            and row.get("actor_id") == event.get("actor_id")
                            and row.get("tick_index") == event.get("tick_index")]
                if len(matching) != 1 or (len(matching) == 1 and type(event.get("sequence")) is int
                                          and type(matching[0].get("sequence")) is int
                                          and event["sequence"] <= matching[0]["sequence"]):
                    errors.append("unverified ingest fatal lacks a prior ingest reservation")
            if isinstance(unverified, Mapping):
                later = [row for row in events if isinstance(row, Mapping)
                         and type(row.get("sequence")) is int and unverified_events
                         and type(unverified_events[0].get("sequence")) is int
                         and row["sequence"] > unverified_events[0]["sequence"]
                          and ((isinstance(row.get("event"), str)
                                and row.get("event") in {"due", "request"})
                              or (row.get("event") == "ingest" and row.get("status") == "admitted"))]
                if later:
                    errors.append("unverified fatal trace contains a later tick or ingest admission")
        elif receipt is None:
            if fatal_events:
                errors.append("fatal QC event is missing its top-level receipt")
            if fatal_status is not None or artifact.get("fatal_reason") is not None:
                errors.append("fatal status is set without a fatal receipt")
            if artifact.get("scientifically_eligible") is not True or artifact.get("censored") is not False:
                errors.append("nonfatal trace eligibility marker is invalid")
            if unverified is not None:
                errors.append("unverified failure is set without its fatal marker")
        else:
            errors.extend(_validate_fatal_receipt(receipt))
            if not isinstance(receipt, Mapping):
                receipt = {}
            if fatal_status != receipt.get("status"):
                errors.append("top-level fatal status does not match receipt")
            expected_reason = ("capacity_exhausted" if receipt.get("phase") == "capacity_exhausted"
                               else receipt.get("status"))
            if artifact.get("fatal_reason") != expected_reason:
                errors.append("top-level fatal reason does not match receipt")
            if artifact.get("scientifically_eligible") is not False or artifact.get("censored") is not True:
                errors.append("fatal trace eligibility marker is invalid")
            if len(fatal_events) != 1:
                errors.append("fatal trace must contain exactly one fatal QC event")
            elif isinstance(fatal_events[0], Mapping):
                fatal_event = fatal_events[0]
                if fatal_event.get("fatal_receipt") != receipt:
                    errors.append("fatal QC receipt differs from top-level receipt")
                if "raw_observations" in receipt:
                    if (fatal_event.get("raw_observations") != receipt.get("raw_observations")
                            or fatal_event.get("raw_cell_count") != receipt.get("raw_cell_count")):
                        errors.append("fatal QC raw observation account differs from receipt")
                elif "raw_observations" in fatal_event or "raw_cell_count" in fatal_event:
                    errors.append("fatal QC fabricates a raw observation account")
                if fatal_event.get("fatal_status") != receipt.get("status") or fatal_event.get("status") != receipt.get("status"):
                    errors.append("fatal QC status does not match receipt")
                if fatal_event.get("reason") != expected_reason:
                    errors.append("fatal QC reason does not identify fatal status")
                for field in ("actor_id", "tick_index", "capture_seq", "committed_cells",
                              "failing_cell_index", "failing_coordinate", "phase",
                              "orphan_provenance_id", "orphan_provenance"):
                    if fatal_event.get(field) != receipt.get(field):
                        errors.append(f"fatal QC {field} differs from receipt")
                if fatal_event.get("scientifically_eligible") is not False or fatal_event.get("censored") is not True:
                    errors.append("fatal QC does not mark scientific ineligibility")
                if receipt.get("phase") == "capacity_exhausted" and (
                    receipt.get("status") != "sensor_ingest_fatal" or fatal_event.get("tick_mutations") != 0
                    or fatal_event.get("committed_cells") != [] or fatal_event.get("reason") != "capacity_exhausted"):
                    errors.append("capacity fatal QC does not declare zero mutations")
                fatal_sequence = fatal_event.get("sequence")
                actor, tick = receipt.get("actor_id"), receipt.get("tick_index")
                admitted = [row for row in events if isinstance(row, Mapping)
                            and row.get("event") == "ingest" and row.get("status") == "admitted"
                            and row.get("actor_id") == actor and row.get("tick_index") == tick]
                if len(admitted) != 1:
                    errors.append("fatal receipt lacks one matching ingest reservation")
                elif (type(fatal_sequence) is not int or type(admitted[0].get("sequence")) is not int
                      or fatal_sequence <= admitted[0]["sequence"]
                      or type(fatal_event.get("controller_monotonic_ns")) is not int
                      or type(admitted[0].get("controller_monotonic_ns")) is not int
                      or fatal_event["controller_monotonic_ns"] < admitted[0]["controller_monotonic_ns"]):
                    errors.append("fatal QC is not after its controller ingest reservation")
                post = [row for row in events if isinstance(row, Mapping)
                        and type(fatal_sequence) is int and type(row.get("sequence")) is int
                        and row["sequence"] > fatal_sequence
                         and ((isinstance(row.get("event"), str)
                               and row.get("event") in {"due", "request"})
                             or (row.get("event") == "ingest" and row.get("status") == "admitted"))]
                if post:
                    errors.append("fatal trace contains a later tick or ingest admission")
            for index, event in enumerate(secondary):
                receipt2 = event.get("secondary_fatal_receipt")
                errors.extend(f"secondary fatal receipt {index}: {error}" for error in _validate_fatal_receipt(receipt2))
                if not isinstance(receipt2, Mapping):
                    continue
                if (event.get("event") != "qc" or event.get("status") != "diagnostic_only"
                        or event.get("reason") != "secondary_fatal_inventory"):
                    errors.append(f"secondary fatal receipt {index} is not diagnostic-only")
                for field in ("actor_id", "tick_index", "capture_seq", "committed_cells",
                              "failing_cell_index", "failing_coordinate", "phase"):
                    if event.get(field) != receipt2.get(field):
                        errors.append(f"secondary fatal receipt {index} {field} mismatch")
                if "raw_observations" in receipt2:
                    if (event.get("raw_observations") != receipt2.get("raw_observations")
                            or event.get("raw_cell_count") != receipt2.get("raw_cell_count")):
                        errors.append(f"secondary fatal receipt {index} raw observation account mismatch")
                matching = [row for row in events if isinstance(row, Mapping)
                            and row.get("event") == "ingest" and row.get("status") == "admitted"
                            and row.get("actor_id") == receipt2.get("actor_id")
                            and row.get("tick_index") == receipt2.get("tick_index")]
                if len(matching) != 1:
                    errors.append(f"secondary fatal receipt {index} lacks ingest reservation")

    return {"valid": not errors, "errors": errors, "event_count": len(events)}


__all__ = [
    "CADENCE_NS", "DEADLINE_NS", "MAX_FATAL_COMMITTED_CELLS", "MAX_TRACE_EVENTS",
    "POLICY", "REQUEST_KEYS", "REQUEST_REQUIRED_KEYS", "TRACE_EVENTS_PER_ACTOR_TICK",
    "REQUEST_SCHEMA", "TRACE_EVENTS", "TRACE_SCHEMA", "validate_hold_trace",
    "validate_request_payload",
]
