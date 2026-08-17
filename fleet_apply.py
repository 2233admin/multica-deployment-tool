"""Ordered, resumable apply orchestration for the one-node fleet contract.

This module owns sequencing and state only.  The actual Multica, AGX, and
official Multica CLI operations are injected as adapters so the orchestration
can be tested without SSH, Docker, or credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from fleet_plan import CONTRACT_VERSION, FleetPlanError, validate_contract


PHASES = ("multica", "agx", "connector", "preflight")
_SECRET_FIELD = re.compile(
    r"(?:secret|token|password|passwd|credential|private[_-]?key|"
    r"api[_-]?key|access[_-]?key|refresh[_-]?token|ssh[_-]?(?:key|private))",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"(?i)(\b(?:secret|token|password|passwd|credential|private[_-]?key|"
    r"api[_-]?(?:key|token)|access[_-]?key|refresh[_-]?token)\b\s*[:=]\s*)\S+"
)
_QUOTED_SECRET_TEXT = re.compile(
    r"(?i)([\"']?(?:secret|token|password|passwd|credential|private[_-]?key|"
    r"api[_-]?(?:key|token)|access[_-]?key|refresh[_-]?token)[\"']?\s*:\s*[\"']?)[^,}\"']+([\"']?)"
)


class FleetApplyError(RuntimeError):
    """A state or contract error prevented apply from starting."""

    def __init__(self, message: str, *, code: str = "apply_error"):
        super().__init__(message)
        self.code = code


Mutation = Callable[..., Any]
ReadOnlyCheck = Callable[..., Any]


@dataclass(frozen=True)
class FleetApplyAdapters:
    """Callbacks for each explicit repository boundary.

    ``reconcile`` is deliberately separate from the mutation phases.  It is
    required before a completed local state record can produce a no-op.
    """

    multica: Mutation
    agx: Mutation
    connector: Mutation
    preflight: Mutation
    reconcile: ReadOnlyCheck | None = None


def contract_digest(contract: Mapping[str, Any]) -> str:
    """Return a stable identifier for the desired state, without secrets."""

    encoded = json.dumps(
        contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def default_state_path(contract: Mapping[str, Any]) -> Path:
    """Use a per-contract local state file so unrelated applies cannot collide."""

    digest = contract_digest(contract)[:16]
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        root = Path(base) / "Multica" if base else Path.home() / "Multica"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "multica"
    return root / f"fleet-apply-{digest}.json"


def _redact_text(value: str) -> str:
    value = _SECRET_TEXT.sub(r"\1<redacted>", value)
    return _QUOTED_SECRET_TEXT.sub(r"\1<redacted>\2", value)


def redact(value: Any) -> Any:
    """Redact adapter details before they enter local state or CLI output."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if isinstance(key, str) and _SECRET_FIELD.search(key):
                result[key] = "<redacted>"
            else:
                result[str(key)] = redact(child)
        return result
    if isinstance(value, list):
        return [redact(child) for child in value]
    if isinstance(value, tuple):
        return [redact(child) for child in value]
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return _redact_text(repr(value))


def _read_state(path: Path, digest: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetApplyError(
            f"unable to read fleet apply state: {path}", code="state_file"
        ) from exc
    if not isinstance(payload, dict):
        raise FleetApplyError("fleet apply state must be a JSON object", code="state_file")
    if payload.get("contract_digest") != digest:
        raise FleetApplyError(
            "fleet apply state belongs to a different contract; choose a new --state-file",
            code="state_contract_mismatch",
        )
    phases = payload.get("phases")
    if not isinstance(phases, dict):
        raise FleetApplyError("fleet apply state has no phase records", code="state_file")
    return payload


def _write_state(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        )
        temporary = Path(handle.name)
        try:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        finally:
            handle.close()
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
        if os.name != "nt":
            path.chmod(0o600)
    except OSError as exc:
        raise FleetApplyError(f"unable to write fleet apply state: {path}", code="state_file") from exc


def _initial_state(
    contract: Mapping[str, Any], contract_path: Path, state_path: Path, retry_command: str
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "contract_digest": contract_digest(contract),
        "contract_path": str(contract_path),
        "state_path": str(state_path),
        "status": "planned",
        "last_completed_phase": None,
        "phases": {
            phase: {"status": "pending"} for phase in PHASES
        },
        "retry_command": retry_command,
    }


def _phase_result(phase: str, status: str, details: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"name": phase, "status": status}
    if details not in (None, {}):
        result["details"] = redact(details)
    return result


def _reconcile_passed(details: Any) -> bool:
    """Interpret a read-only check without requiring a response schema."""

    if details is False:
        return False
    if isinstance(details, Mapping) and details.get("ready") is False:
        return False
    return True


def _reset_completed_state(state: dict[str, Any]) -> None:
    state["status"] = "planned"
    state["last_completed_phase"] = None
    state.pop("failed_phase", None)
    state["phases"] = {phase: {"status": "pending"} for phase in PHASES}


def apply_contract(
    contract: Mapping[str, Any],
    adapters: FleetApplyAdapters,
    *,
    contract_path: Path | None = None,
    state_path: Path | None = None,
    retry_command: str | None = None,
    reconcile: ReadOnlyCheck | None = None,
) -> dict[str, Any]:
    """Apply a validated one-node contract in order and resume safely.

    A phase is marked complete only after its adapter returns.  If a later
    phase fails, completed phases remain recorded and a retry invokes only the
    failed phase and those after it.  A fully completed contract is a no-op.
    """

    try:
        normalized = validate_contract(contract)
    except FleetPlanError:
        raise
    selected_path = (contract_path or Path("contract.json")).expanduser().resolve()
    selected_state = (state_path or default_state_path(normalized)).expanduser().resolve()
    command = retry_command or (
        f'python multica_deploy.py fleet apply --contract "{selected_path}" '
        f'--state-file "{selected_state}" --resume'
    )
    digest = contract_digest(normalized)
    state = _read_state(selected_state, digest)
    if state is None:
        state = _initial_state(normalized, selected_path, selected_state, command)
        _write_state(selected_state, state)
    else:
        state["retry_command"] = command

    node = normalized["nodes"][0]
    reconcile_callback = reconcile or adapters.reconcile
    all_phases_completed = all(
        state["phases"].get(phase, {}).get("status") == "completed"
        for phase in PHASES
    )
    if all_phases_completed:
        if reconcile_callback is None:
            raise FleetApplyError(
                "completed fleet state requires a read-only reconcile check before no-op",
                code="reconcile_required",
            )
        try:
            reconcile_details = reconcile_callback(normalized, node)
        except Exception as exc:
            message = _redact_text(str(exc)) or exc.__class__.__name__
            raise FleetApplyError(
                f"fleet readiness check failed: {message}", code="reconcile_failed"
            ) from exc
        if not _reconcile_passed(reconcile_details):
            # A successful read-only check is the only path to no-op.  A
            # negative result means the local receipt is stale, so replay the
            # ordered phases from a clean pending state.
            _reset_completed_state(state)
            _write_state(selected_state, state)
        else:
            rendered_phases = [
                _phase_result(phase, "skipped", state["phases"][phase].get("result"))
                for phase in PHASES
            ]
            return {
                "status": "configured",
                "contract_version": CONTRACT_VERSION,
                "node": node["name"],
                "last_completed_phase": state["last_completed_phase"],
                "phases": rendered_phases,
                "retry_command": command,
                "state_file": str(selected_state),
                "no_op": True,
                "reconcile": redact(reconcile_details),
            }

    phase_callbacks: dict[str, tuple[Mutation, tuple[Any, ...]]] = {
        "multica": (adapters.multica, (normalized,)),
        "agx": (adapters.agx, (normalized, node)),
        "connector": (adapters.connector, (normalized, node)),
        "preflight": (adapters.preflight, (normalized, node)),
    }
    rendered_phases: list[dict[str, Any]] = []
    failed_phase: str | None = None

    for phase in PHASES:
        record = state["phases"].get(phase, {})
        if record.get("status") == "completed":
            rendered_phases.append(_phase_result(phase, "skipped", record.get("result")))
            continue

        failed_phase = phase
        state["status"] = "applying"
        record = {"status": "running"}
        state["phases"][phase] = record
        _write_state(selected_state, state)
        callback, callback_args = phase_callbacks[phase]
        try:
            details = callback(*callback_args)
        except Exception as exc:  # adapter failures become resumable apply results
            message = _redact_text(str(exc)) or exc.__class__.__name__
            record.update({"status": "failed", "error": message})
            state["status"] = "failed"
            state["failed_phase"] = phase
            _write_state(selected_state, state)
            rendered_phases.append(_phase_result(phase, "failed", {"error": message}))
            return {
                "status": "failed",
                "contract_version": CONTRACT_VERSION,
                "node": node["name"],
                "last_completed_phase": state.get("last_completed_phase"),
                "failed_phase": phase,
                "phases": rendered_phases,
                "retry_command": command,
                "state_file": str(selected_state),
            }
        record.update({"status": "completed", "result": redact(details)})
        state["phases"][phase] = record
        state["last_completed_phase"] = phase
        state.pop("failed_phase", None)
        _write_state(selected_state, state)
        rendered_phases.append(_phase_result(phase, "completed", details))
        failed_phase = None

    state["status"] = "configured"
    state["last_completed_phase"] = PHASES[-1]
    _write_state(selected_state, state)
    return {
        "status": "configured",
        "contract_version": CONTRACT_VERSION,
        "node": node["name"],
        "last_completed_phase": state["last_completed_phase"],
        "phases": rendered_phases,
        "retry_command": command,
        "state_file": str(selected_state),
        "no_op": all(phase["status"] == "skipped" for phase in rendered_phases),
    }


def render(result: Mapping[str, Any], output_format: str = "human") -> str:
    """Render a stable, secret-free apply result."""

    if output_format == "json":
        return json.dumps(redact(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    lines = [
        f"Fleet apply: {result['status']}",
        f"node: {result.get('node', 'unknown')}",
        f"last_completed_phase: {result.get('last_completed_phase') or 'none'}",
    ]
    if result.get("failed_phase"):
        lines.append(f"failed_phase: {result['failed_phase']}")
    for phase in result.get("phases", []):
        lines.append(f"  {phase['name']}: {phase['status']}")
    if result.get("retry_command"):
        lines.append(f"retry: {result['retry_command']}")
    if result.get("state_file"):
        lines.append(f"state_file: {result['state_file']}")
    return "\n".join(lines) + "\n"


def error_result(exc: Exception) -> dict[str, Any]:
    code = getattr(exc, "code", "apply_error")
    return {"status": "invalid", "error": {"code": code, "message": _redact_text(str(exc))}}
