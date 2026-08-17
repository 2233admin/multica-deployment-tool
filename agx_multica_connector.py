"""Small, fail-closed seam for the versioned official Multica CLI.

This module deliberately owns transport and response validation only.  It does
not contact Multica by HTTP, persist credentials, or turn a local result into
an end-to-end ``verified`` claim.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


CONNECTOR_VERSION = "agx-multica-connector/v1"
_TASK_FIELDS = (
    "task_id",
    "repository",
    "ref",
    "environment",
    "action",
    "target_selector",
)
_REQUIRED_RESPONSE_FIELDS = (
    "schema",
    "cli_version",
    "task_id",
    "deployment_id",
    "node_identity",
    "status",
    "health",
    "rollback_available",
    "summary",
)
_SENSITIVE_KEY = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|access[_-]?key|"
    r"authorization|cookie|private[_-]?key|credential)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(bearer\s+|(?:token|secret|password|passwd|api[_-]?key|"
    r"access[_-]?key|authorization)\s*[:=]\s*)[^\s,;]+"
)


@dataclass(frozen=True)
class CommandResult:
    """The only process data the connector consumes from its runner."""

    returncode: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True)
class ConnectorResult:
    """Typed, redacted outcome of one connector invocation."""

    ok: bool
    code: str
    message: str = ""
    payload: Mapping[str, Any] | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class MulticaCliConfig:
    """Explicit pin for the official CLI command and response contract."""

    command: tuple[str, ...]
    version: str
    json_schema: str
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if isinstance(self.command, (str, bytes)):
            raise ValueError("command must be an argument sequence")
        normalized_command = tuple(self.command)
        if not normalized_command or any(
            not isinstance(part, str) or not part or _has_control(part)
            for part in normalized_command
        ):
            raise ValueError("command must contain non-empty safe arguments")
        object.__setattr__(self, "command", normalized_command)
        if not _safe_nonempty(self.version):
            raise ValueError("version must be a non-empty safe string")
        if not _safe_nonempty(self.json_schema):
            raise ValueError("json_schema must be a non-empty safe string")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")


@dataclass(frozen=True)
class TaskReference:
    """Secret-free task data passed to the CLI as structured arguments."""

    task_id: str
    repository: str
    ref: str
    environment: str
    action: str
    target_selector: str


Runner = Callable[[Sequence[str], float], CommandResult]


def run_official_multica_cli(
    argv: Sequence[str], timeout_seconds: float
) -> CommandResult:
    """Run exactly the configured argv without a shell or interactive input."""

    completed = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        shell=False,
        timeout=timeout_seconds,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


class AgxMulticaConnector:
    """Invoke and validate one versioned official Multica CLI operation."""

    def __init__(self, config: MulticaCliConfig, *, runner: Runner | None = None):
        if not isinstance(config, MulticaCliConfig):
            raise TypeError("config must be MulticaCliConfig")
        self.config = config
        self._runner = runner or run_official_multica_cli
        if not callable(self._runner):
            raise TypeError("runner must be callable")

    def compatibility(self) -> ConnectorResult:
        """Return configured compatibility metadata; this does not probe a live CLI."""

        return ConnectorResult(
            ok=True,
            code="compatible",
            payload={
                "connector_version": CONNECTOR_VERSION,
                "cli_version": self.config.version,
                "json_schema": self.config.json_schema,
                "compatible": True,
            },
        )

    def execute(self, task: TaskReference | Mapping[str, Any]) -> ConnectorResult:
        normalized_task, task_error = _normalize_task(task)
        if task_error:
            return _failure("invalid_task", task_error)

        argv = self._build_argv(normalized_task)
        try:
            command_result = _coerce_command_result(
                self._runner(argv, self.config.timeout_seconds)
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            return _failure("timeout", "official CLI timed out")
        except OSError:
            return _failure("runner_error", "official CLI could not be started")
        except Exception:
            # A runner must not be able to leak its exception, command line, or
            # process output through this boundary.
            return _failure("runner_error", "official CLI runner failed")

        if command_result.returncode != 0:
            return _failure(
                "nonzero_exit",
                "official CLI exited non-zero",
                exit_code=command_result.returncode,
            )
        if command_result.stderr.strip():
            return _failure("human_output", "official CLI wrote non-JSON output")

        try:
            decoded = json.loads(command_result.stdout)
        except (TypeError, json.JSONDecodeError):
            return _failure("invalid_json", "official CLI stdout is not valid JSON")
        if not isinstance(decoded, dict):
            return _failure("invalid_schema", "official CLI JSON must be an object")

        contract_error = _validate_response(decoded, self.config, normalized_task)
        if contract_error:
            return _failure(contract_error[0], contract_error[1])

        payload = {
            key: _redact(decoded[key])
            for key in _REQUIRED_RESPONSE_FIELDS
        }
        return ConnectorResult(ok=True, code="ok", payload=payload)

    __call__ = execute

    def _build_argv(self, task: TaskReference) -> list[str]:
        # These are values, never a shell command.  Version and schema are
        # pinned in config and sent explicitly so an unpinned CLI cannot pass.
        argv = [
            *self.config.command,
            "--version",
            self.config.version,
            "--schema",
            self.config.json_schema,
            "--output",
            "json",
        ]
        for field in _TASK_FIELDS:
            argv.extend((f"--{field.replace('_', '-')}", getattr(task, field)))
        return argv


def _normalize_task(
    task: TaskReference | Mapping[str, Any],
) -> tuple[TaskReference | None, str | None]:
    if isinstance(task, TaskReference):
        values = {field: getattr(task, field) for field in _TASK_FIELDS}
    elif isinstance(task, Mapping):
        if set(task) != set(_TASK_FIELDS):
            return None, "task fields do not match the secret-free connector contract"
        values = dict(task)
    else:
        return None, "task must be a TaskReference or mapping"

    if any(not isinstance(values[field], str) or not _safe_nonempty(values[field]) for field in _TASK_FIELDS):
        return None, "task fields must be non-empty safe strings"
    return TaskReference(**values), None


def _validate_response(
    payload: Mapping[str, Any],
    config: MulticaCliConfig,
    task: TaskReference,
) -> tuple[str, str] | None:
    for field in _REQUIRED_RESPONSE_FIELDS:
        if field not in payload:
            return "missing_field", f"response missing required field: {field}"

    if payload["schema"] != config.json_schema:
        return "schema_mismatch", "response JSON schema does not match configured schema"
    if payload["cli_version"] != config.version:
        return "version_mismatch", "response CLI version does not match configured version"
    if payload["task_id"] != task.task_id:
        return "task_mismatch", "response task ID does not match requested task"

    for field in (
        "schema",
        "cli_version",
        "task_id",
        "deployment_id",
        "node_identity",
        "status",
        "health",
    ):
        if not isinstance(payload[field], str) or not _safe_nonempty(payload[field]):
            return "invalid_schema", f"response field has invalid type: {field}"
    if payload["status"].strip().lower() not in {"completed", "success"}:
        return "invalid_status", "response status is not completed or success"
    if payload["health"].strip().lower() not in {"healthy", "ok"}:
        return "invalid_health", "response health is not healthy or ok"
    if not isinstance(payload["rollback_available"], bool):
        return "invalid_schema", "response field has invalid type: rollback_available"
    if not isinstance(payload["summary"], Mapping):
        return "invalid_schema", "response field has invalid type: summary"
    return None


def _coerce_command_result(value: CommandResult) -> CommandResult:
    if isinstance(value, CommandResult):
        return value
    # Permit a lightweight injected fake with CompletedProcess-like fields,
    # while keeping the public boundary explicit and typed after coercion.
    return CommandResult(
        returncode=int(value.returncode),
        stdout=_text(value.stdout),
        stderr=_text(value.stderr),
    )


def _failure(code: str, message: str, *, exit_code: int | None = None) -> ConnectorResult:
    return ConnectorResult(
        ok=False,
        code=code,
        message=_redact_text(message),
        exit_code=exit_code,
    )


def _redact(value: Any, *, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    return _SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}[REDACTED]", value)


def _safe_nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not _has_control(value)


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else str(value)


__all__ = [
    "AgxMulticaConnector",
    "CommandResult",
    "CONNECTOR_VERSION",
    "ConnectorResult",
    "MulticaCliConfig",
    "TaskReference",
    "run_official_multica_cli",
]
