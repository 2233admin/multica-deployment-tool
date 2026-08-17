"""Fail-closed, reader-injected verification for one fleet node.

This module is deliberately an orchestration seam, not a transport adapter. It
does not use HTTP, SSH, Docker, credentials, or a live Multica/AGX client. The
caller injects readers for structured evidence and a callback that creates or
reuses one disposable task.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


VERIFY_SCHEMA = "fleet-verify/v1"
_PREFLIGHT_SECTIONS = ("health", "readiness", "auth", "workspace", "runtime")
_AGX_SECTIONS = ("installation", "version", "bundle", "node", "lifecycle")
_TASK_FIELDS = ("task_id", "deployment_id", "node_identity", "status", "health")
_SENSITIVE_KEY = re.compile(
    r"(?:token|secret|password|passwd|credential|authorization|cookie|"
    r"private[_-]?key|api[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:bearer\s+|(?:token|secret|password|passwd|credential|"
    r"authorization|cookie|private[_-]?key|api[_-]?key|access[_-]?key)"
    r"\s*[:=])\S+"
)


EvidenceReader = Callable[[Mapping[str, Any]], Mapping[str, Any]]
TaskRunner = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class FleetVerifyAdapters:
    """Injected external seams used by :class:`FleetVerifier`.

    Each callback may accept the read-only context mapping or no arguments.
    The task callback is called at most once per ``verify`` invocation and is
    always given ``context["task"]["disposable"] == True``.
    """

    multica_reader: EvidenceReader
    agx_reader: EvidenceReader
    task_runner: TaskRunner


@dataclass(frozen=True)
class _Issue:
    code: str
    status: str
    message: str


class FleetVerifier:
    """Verify one node using only injected structured evidence.

    ``verify`` is deterministic for the same callback readbacks. It does not
    cache a successful result and does not create permanent task state itself;
    repeat calls invoke the injected disposable-task seam again.
    """

    def __init__(
        self,
        multica_reader: EvidenceReader | None = None,
        agx_reader: EvidenceReader | None = None,
        task_runner: TaskRunner | None = None,
        *,
        adapters: FleetVerifyAdapters | None = None,
    ) -> None:
        if adapters is not None:
            if any(value is not None for value in (multica_reader, agx_reader, task_runner)):
                raise TypeError("pass adapters or individual callbacks, not both")
            multica_reader = adapters.multica_reader
            agx_reader = adapters.agx_reader
            task_runner = adapters.task_runner
        if not all(callable(value) for value in (multica_reader, agx_reader, task_runner)):
            raise TypeError("multica_reader, agx_reader, and task_runner are required callables")
        self._multica_reader = multica_reader
        self._agx_reader = agx_reader
        self._task_runner = task_runner

    def verify(
        self,
        contract: Mapping[str, Any] | None = None,
        *,
        node: Mapping[str, Any] | None = None,
        task: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one read-only preflight and one disposable task verification.

        ``contract`` is the normalized fleet contract when available. Only its
        redacted form is passed to callbacks. ``node`` and ``task`` override
        the first contract node and derived disposable task request; a task
        request cannot opt out of being disposable.
        """

        safe_contract = _redact(contract if isinstance(contract, Mapping) else {})
        contract_node = _first_node(contract)
        contract_identity = _text_value(contract_node.get("node_identity"))
        if contract_identity is None:
            return _result(_missing("contract.nodes[0].node_identity"))
        selected_node = node if isinstance(node, Mapping) else contract_node
        safe_node = _redact(selected_node or {})
        expected_identities = _stable_node_identities(contract, selected_node)
        if len(expected_identities) > 1:
            return _result(_mismatch("contract/node node_identity"))
        task_request, request_issue = _task_request(contract, task)
        if request_issue is not None:
            return _result(request_issue)

        context = {
            "contract": safe_contract,
            "node": safe_node,
            "task": _redact(task_request),
        }

        # Collect both read-only sides before deciding whether the task gate
        # can run. This keeps a missing runtime diagnostically useful without
        # allowing a partial preflight to trigger a disposable task.
        multica_raw, multica_issue = _read_callback(self._multica_reader, context, "multica")
        agx_raw, agx_issue = _read_callback(self._agx_reader, context, "agx")
        multica = None
        agx = None
        if multica_issue is None:
            multica, multica_issue = _validate_multica(multica_raw, contract)
        if agx_issue is None:
            agx, agx_issue = _validate_agx(agx_raw, contract, selected_node)
        if multica_issue is not None:
            return _result(multica_issue, agx=agx)
        if agx_issue is not None:
            return _result(agx_issue, multica=multica)

        task_raw, issue = _read_callback(self._task_runner, context, "task")
        if issue is not None:
            return _result(issue, multica=multica, agx=agx)
        task_summary, issue = _validate_task(task_raw)
        if issue is not None:
            return _result(issue, multica=multica, agx=agx)

        # A task receipt is not enough by itself: its node must be the same
        # node whose AGX evidence just passed verification.  Also bind it to
        # every stable identity supplied by the contract/request so a valid
        # receipt cannot be replayed against a different node.
        verified_agx_identity = _text_value(
            ((agx_raw or {}).get("node") or {}).get("node_identity")
        )
        if verified_agx_identity is None:
            return _result(_missing("agx.node.node_identity"), multica=multica, agx=agx)
        if task_summary["node_identity"] != verified_agx_identity:
            return _result(_mismatch("task/AGX node_identity"), multica=multica, agx=agx)
        if expected_identities and task_summary["node_identity"] != expected_identities[0]:
            return _result(_mismatch("task/contract node_identity"), multica=multica, agx=agx)

        result = _result(
            _Issue("verified", "verified", "all required live evidence agrees"),
            multica=multica,
            agx=agx,
            task=task_summary,
        )
        # These values have already passed the strict task-record validator.
        result.update(
            {
                "task_id": task_summary["task_id"],
                "deployment_id": task_summary["deployment_id"],
                "node_identity": task_summary["node_identity"],
            }
        )
        return result

    __call__ = verify


def verify_one_node(
    contract: Mapping[str, Any] | None = None,
    *,
    multica_reader: EvidenceReader,
    agx_reader: EvidenceReader,
    task_runner: TaskRunner,
    node: Mapping[str, Any] | None = None,
    task: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Functional entry point for a future ``fleet verify`` CLI adapter."""

    return FleetVerifier(
        multica_reader=multica_reader,
        agx_reader=agx_reader,
        task_runner=task_runner,
    ).verify(contract, node=node, task=task)


def render(result: Mapping[str, Any]) -> str:
    """Serialize a result without exposing callback payloads or secrets."""

    import json

    return json.dumps(_redact(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read_callback(
    callback: Callable[..., Any], context: Mapping[str, Any], name: str
) -> tuple[Any, _Issue | None]:
    try:
        value = _invoke(callback, context)
    except Exception:
        return None, _Issue("reader_error", "blocked", f"{name} evidence reader failed")
    if not isinstance(value, Mapping):
        return None, _Issue("missing_evidence", "awaiting_verification", f"{name} evidence is not structured")
    return value, None


def _invoke(callback: Callable[..., Any], context: Mapping[str, Any]) -> Any:
    """Support zero-argument test doubles without masking callback failures."""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(context)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if not positional and not has_varargs:
        return callback()
    return callback(context)


def _validate_multica(
    raw: Mapping[str, Any], contract: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, _Issue | None]:
    sections: dict[str, Mapping[str, Any]] = {}
    for name in _PREFLIGHT_SECTIONS:
        value = raw.get(name)
        if not isinstance(value, Mapping):
            return None, _missing(f"multica.{name}")
        if _is_mock(value):
            return None, _mock(f"multica.{name}")
        sections[name] = value

    checks = (
        ("health", _healthy),
        ("readiness", _ready),
        ("auth", _authenticated),
        ("workspace", _available),
        ("runtime", _online),
    )
    for name, predicate in checks:
        if not predicate(sections[name]):
            return None, _invalid(f"multica.{name}")

    workspace_id = _text_value(sections["workspace"].get("workspace_id"))
    if workspace_id is None:
        return None, _missing("multica.workspace.workspace_id")
    expected_workspace = _contract_value(contract, "multica", "workspace_id")
    if expected_workspace is not None and workspace_id != expected_workspace:
        return None, _mismatch("workspace_id")
    if _text_value(sections["runtime"].get("runtime_id")) is None:
        return None, _missing("multica.runtime.runtime_id")

    return {
        "health": _status_value(sections["health"], "healthy"),
        "readiness": _status_value(sections["readiness"], "ready"),
        "auth": _status_value(sections["auth"], "authenticated"),
        "workspace": _status_value(sections["workspace"], "available"),
        "workspace_id": _public_id(workspace_id),
        "runtime": _status_value(sections["runtime"], "online"),
    }, None


def _validate_agx(
    raw: Mapping[str, Any],
    contract: Mapping[str, Any] | None,
    node: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, _Issue | None]:
    sections: dict[str, Mapping[str, Any]] = {}
    for name in _AGX_SECTIONS:
        value = raw.get(name)
        if not isinstance(value, Mapping):
            return None, _missing(f"agx.{name}")
        if _is_mock(value):
            return None, _mock(f"agx.{name}")
        sections[name] = value

    installation_id = _text_value(sections["installation"].get("installation_id"))
    bundle_id = _text_value(sections["bundle"].get("bundle_id"))
    node_identity = _text_value(sections["node"].get("node_identity"))
    versions = [
        _text_value(sections["installation"].get("version")),
        _text_value(sections["version"].get("version")),
        _text_value(sections["bundle"].get("version")),
    ]
    if installation_id is None:
        return None, _missing("agx.installation.installation_id")
    if bundle_id is None:
        return None, _missing("agx.bundle.bundle_id")
    if node_identity is None:
        return None, _missing("agx.node.node_identity")
    if any(version is None for version in versions):
        return None, _missing("agx.version.version")
    if len(set(versions)) != 1:
        return None, _mismatch("agx version")
    if not _installed(sections["installation"]):
        return None, _invalid("agx.installation")
    if not _installed(sections["bundle"]):
        return None, _invalid("agx.bundle")
    if not _registered(sections["node"]):
        return None, _invalid("agx.node")
    if not _lifecycle_ready(sections["lifecycle"]):
        return None, _invalid("agx.lifecycle")

    expected_version = _contract_value(contract, "agx", "version")
    if expected_version is not None and versions[0] != expected_version:
        return None, _mismatch("agx version")
    expected_node = _text_value((node or {}).get("node_identity"))
    contract_node = _first_node(contract)
    expected_contract_node = _text_value(contract_node.get("node_identity"))
    if expected_contract_node is not None and node_identity != expected_contract_node:
        return None, _mismatch("node_identity")
    if expected_node is not None and node_identity != expected_node:
        return None, _mismatch("node_identity")

    return {
        "installation": _status_value(sections["installation"], "installed"),
        "installation_id": _public_id(installation_id),
        "version": versions[0],
        "bundle": _status_value(sections["bundle"], "installed"),
        "bundle_id": _public_id(bundle_id),
        "node": _status_value(sections["node"], "registered"),
        "node_identity": _public_id(node_identity),
        "lifecycle": _status_value(sections["lifecycle"], "ready"),
    }, None


def _validate_task(raw: Mapping[str, Any]) -> tuple[dict[str, Any] | None, _Issue | None]:
    if _is_mock(raw):
        return None, _mock("task")
    sides: dict[str, dict[str, str]] = {}
    for side in ("multica", "agx"):
        value = raw.get(side)
        if not isinstance(value, Mapping):
            return None, _Issue(
                "missing_task_evidence", "awaiting_verification", f"task.{side} evidence is missing"
            )
        if _is_mock(value):
            return None, _mock(f"task.{side}")
        records: list[dict[str, str]] = []
        for record_name in ("receipt", "readback"):
            record = value.get(record_name)
            if not isinstance(record, Mapping):
                return None, _Issue(
                    "missing_task_evidence",
                    "awaiting_verification",
                    f"task.{side}.{record_name} evidence is missing",
                )
            if _is_mock(record):
                return None, _mock(f"task.{side}.{record_name}")
            normalized, issue = _task_record(record, side, record_name)
            if issue is not None:
                return None, issue
            records.append(normalized)
        if records[0] != records[1]:
            return None, _mismatch(f"task.{side} receipt/readback")
        sides[side] = records[0]

    if sides["multica"] != sides["agx"]:
        return None, _mismatch("task Multica/AGX")
    if not sides["multica"]["deployment_id"]:
        return None, _missing("task.deployment_id")
    return dict(sides["multica"]), None


def _task_record(
    record: Mapping[str, Any], side: str, record_name: str
) -> tuple[dict[str, str], _Issue | None]:
    values: dict[str, str] = {}
    for field in _TASK_FIELDS:
        value = _text_value(record.get(field))
        if value is None:
            return None, _Issue(
                "missing_task_evidence",
                "awaiting_verification",
                f"task.{side}.{record_name}.{field} is missing",
            )
        if _SENSITIVE_TEXT.search(value) or _SENSITIVE_KEY.search(value):
            return None, _Issue("sensitive_evidence", "blocked", "task evidence contains sensitive content")
        values[field] = _canonical_task_value(field, value)
    if values["status"] != "completed":
        return None, _invalid(f"task.{side}.{record_name}.status")
    if values["health"] != "healthy":
        return None, _invalid(f"task.{side}.{record_name}.health")
    return values, None


def _task_request(
    contract: Mapping[str, Any] | None, task: Mapping[str, Any] | None
) -> tuple[dict[str, Any], _Issue | None]:
    if task is not None and not isinstance(task, Mapping):
        return {}, _Issue("invalid_request", "blocked", "task request must be structured")
    request = dict(task or {})
    if request.get("disposable", True) is not True:
        return {}, _Issue("non_disposable_task", "blocked", "verification requires a disposable task")
    project = _first_project(contract)
    for key in ("repository", "ref", "environment"):
        if key not in request and project.get(key) is not None:
            request[key] = project[key]
    request.setdefault("purpose", "fleet-verify")
    request["disposable"] = True
    return request, None


def _result(
    issue: _Issue,
    *,
    multica: Mapping[str, Any] | None = None,
    agx: Mapping[str, Any] | None = None,
    task: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": VERIFY_SCHEMA,
        "status": issue.status,
        "code": issue.code,
        "message": issue.message,
    }
    evidence: dict[str, Any] = {}
    if multica is not None:
        evidence["multica"] = dict(multica)
    if agx is not None:
        evidence["agx"] = dict(agx)
    if task is not None:
        evidence["task"] = dict(task)
    if evidence:
        result["evidence"] = evidence
    return _redact(result)


def _missing(path: str) -> _Issue:
    return _Issue("missing_evidence", "awaiting_verification", f"required evidence is missing: {path}")


def _mock(path: str) -> _Issue:
    return _Issue("mock_evidence", "blocked", f"mock evidence is not admissible: {path}")


def _invalid(path: str) -> _Issue:
    return _Issue("invalid_evidence", "blocked", f"evidence is not healthy or ready: {path}")


def _mismatch(field: str) -> _Issue:
    return _Issue("evidence_mismatch", "blocked", f"structured evidence disagrees: {field}")


def _healthy(value: Mapping[str, Any]) -> bool:
    return _bool_or_status(value, "healthy", "ok")


def _ready(value: Mapping[str, Any]) -> bool:
    return _bool_or_status(value, "ready", "online", "healthy", "ok")


def _authenticated(value: Mapping[str, Any]) -> bool:
    return _bool_or_status(value, "authenticated", "authorized", "ready", "ok")


def _available(value: Mapping[str, Any]) -> bool:
    return _bool_or_status(value, "available", "ready", "online", "ok")


def _online(value: Mapping[str, Any]) -> bool:
    return _bool_or_status(value, "online", "ready", "healthy", "ok")


def _installed(value: Mapping[str, Any]) -> bool:
    return _bool_or_status(value, "installed", "ready", "available", "ok")


def _registered(value: Mapping[str, Any]) -> bool:
    return _bool_or_status(value, "registered", "online", "ready", "active", "ok")


def _lifecycle_ready(value: Mapping[str, Any]) -> bool:
    return _bool_or_status(value, "ready", "online", "running", "active", "initialized", "ok")


def _bool_or_status(value: Mapping[str, Any], *accepted: str) -> bool:
    for key in ("ok", "ready", "authenticated", "available", "online"):
        if key in value and isinstance(value[key], bool):
            if value[key]:
                return True
    status = _text_value(value.get("status")) or _text_value(value.get("state"))
    return status is not None and status.lower() in accepted


def _status_value(value: Mapping[str, Any], fallback: str) -> str:
    status = _text_value(value.get("status")) or _text_value(value.get("state"))
    return (status or fallback).lower()


def _canonical_task_value(field: str, value: str) -> str:
    value = value.lower()
    if field == "status" and value in {"success", "succeeded", "done", "ok", "completed"}:
        return "completed"
    if field == "health" and value in {"ok", "passed", "pass", "success", "healthy"}:
        return "healthy"
    return value


def _is_mock(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"mock", "is_mock"} and child is True:
                return True
            if normalized in {"source", "mode", "evidence_type", "kind"}:
                if isinstance(child, str) and child.lower() in {"mock", "synthetic", "fake"}:
                    return True
            if normalized == "live" and child is False:
                return True
            if _is_mock(child):
                return True
    elif isinstance(value, list):
        return any(_is_mock(child) for child in value)
    return False


def _contract_value(contract: Mapping[str, Any] | None, section: str, key: str) -> str | None:
    if not isinstance(contract, Mapping) or not isinstance(contract.get(section), Mapping):
        return None
    return _text_value(contract[section].get(key))


def _first_node(contract: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if isinstance(contract, Mapping) and isinstance(contract.get("nodes"), list):
        first = contract["nodes"][0] if contract["nodes"] else {}
        return first if isinstance(first, Mapping) else {}
    return {}


def _stable_node_identities(
    contract: Mapping[str, Any] | None, selected_node: Mapping[str, Any] | None
) -> list[str]:
    """Return distinct contract/request node identities in stable order."""

    identities: list[str] = []
    contract_node = _first_node(contract)
    for candidate in (contract_node, selected_node):
        if not isinstance(candidate, Mapping):
            continue
        identity = _text_value(candidate.get("node_identity"))
        if identity is not None and identity not in identities:
            identities.append(identity)
    return identities


def _first_project(contract: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if isinstance(contract, Mapping) and isinstance(contract.get("projects"), list):
        first = contract["projects"][0] if contract["projects"] else {}
        return first if isinstance(first, Mapping) else {}
    return {}


def _text_value(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _public_id(value: str) -> str:
    if _SENSITIVE_TEXT.search(value) or _SENSITIVE_KEY.search(value):
        return "[REDACTED]"
    return value


def _redact(value: Any, *, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(child_key): _redact(child, key=str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, tuple):
        return [_redact(child) for child in value]
    if isinstance(value, str):
        return _SENSITIVE_TEXT.sub("[REDACTED]", value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "[REDACTED]"


__all__ = [
    "EvidenceReader",
    "FleetVerifyAdapters",
    "FleetVerifier",
    "TaskRunner",
    "VERIFY_SCHEMA",
    "render",
    "verify_one_node",
]
