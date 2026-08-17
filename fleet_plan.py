"""Read-only validation and planning for the v1 fleet contract.

This module deliberately has no deployment adapters.  "fleet plan" is the
external seam for the later apply/verify phases, so validating a plan must not
open a network connection or call a mutating deployment function.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping


CONTRACT_VERSION = 1
OUTPUT_HUMAN = "human"
OUTPUT_JSON = "json"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SEMVER = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@-]{0,255}\Z")
_SOURCE_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.IGNORECASE)
_IMAGE_DIGEST = re.compile(
    r"[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?@sha256:[0-9a-f]{64}\Z",
    re.IGNORECASE,
)
_SECRET_FIELD = re.compile(
    r"(?:secret|token|password|passwd|credential|private[_-]?key|"
    r"api[_-]?key|access[_-]?key|refresh[_-]?token|ssh[_-]?(?:key|private))",
    re.IGNORECASE,
)

_TOP_LEVEL_FIELDS = {"contract_version", "multica", "agx", "nodes", "projects"}
_MULTICA_FIELDS = {
    "server_url",
    "profile",
    "workspace_id",
    "backend_image",
    "web_image",
    "source_revision",
}
_AGX_FIELDS = {"version", "installation_root"}
_NODE_FIELDS = {"name", "node_identity", "platform", "labels"}
_PROJECT_FIELDS = {"name", "repository", "ref", "environment"}
# The checked-in connector/bootstrap path currently has a Linux-only apply
# implementation.  Keep plan and apply capabilities aligned so an invalid
# platform cannot reach a mutating phase.
_PLATFORMS = {"linux"}


class FleetPlanError(ValueError):
    """A contract or local prerequisite prevented a fleet plan."""

    def __init__(self, message: str, *, path: str = "", code: str = "invalid_contract"):
        super().__init__(message)
        self.path = path
        self.code = code


def _error(message: str, path: str) -> FleetPlanError:
    return FleetPlanError(message, path=path)


def _expect_object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _error(f"{path} must be an object", path)
    return value


def _expect_string(
    value: Any, path: str, pattern: re.Pattern[str] | None = None
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _error(
            f"{path} must be a non-empty string without surrounding whitespace", path
        )
    if any(ord(char) < 32 for char in value):
        raise _error(f"{path} contains a control character", path)
    if pattern is not None and not pattern.fullmatch(value):
        raise _error(f"{path} has an invalid value", path)
    return value


def _check_fields(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    for key in value:
        if not isinstance(key, str):
            raise _error(f"{path} contains a non-string field name", path)
        if _SECRET_FIELD.search(key):
            raise FleetPlanError(
                f"{path}.{key} is not allowed in the secret-free fleet contract",
                path=f"{path}.{key}",
                code="secret_field",
            )
        if key not in allowed:
            raise _error(f"{path}.{key} is not a supported v1 field", f"{path}.{key}")


def _scan_secret_fields(value: Any, path: str = "$") -> None:
    """Catch credential-like keys before ordinary unknown-field errors."""

    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and _SECRET_FIELD.search(key):
                raise FleetPlanError(
                    f"{path}.{key} is not allowed in the secret-free fleet contract",
                    path=f"{path}.{key}",
                    code="secret_field",
                )
            _scan_secret_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_secret_fields(child, f"{path}[{index}]")


def _validate_origin(value: Any, path: str) -> str:
    value = _expect_string(value, path)
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise _error(f"{path} must be an absolute http(s) origin", path) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise _error(f"{path} must be an absolute http(s) origin", path)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise _error(
            f"{path} must not contain credentials, query, or fragment", path
        )
    if parsed.path not in {"", "/"}:
        raise _error(f"{path} must contain only the server origin", path)
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise _error(f"{path} has an invalid port", path)
    except ValueError as exc:
        raise _error(f"{path} has an invalid port", path) from exc
    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise _error(f"{path} must include a valid host", path) from exc
    if not hostname:
        raise _error(f"{path} must include a host", path)
    return value.rstrip("/")


def _validate_absolute_path(value: Any, path: str) -> str:
    value = _expect_string(value, path)
    # Fleet v1 applies only to Linux nodes.  Keep the contract unambiguous at
    # its boundary: Windows drive paths and UNC paths must never be accepted
    # and later interpreted by a remote shell as a different root.
    is_windows_drive = re.fullmatch(r"[A-Za-z]:[\\/].+", value) is not None
    is_unc = value.startswith(("\\\\", "//"))
    if is_windows_drive or is_unc or not value.startswith("/"):
        raise _error(f"{path} must be an absolute installation path", path)
    return value


def _validate_multica(value: Any) -> dict[str, str]:
    multica = _expect_object(value, "$.multica")
    _check_fields(multica, _MULTICA_FIELDS, "$.multica")
    required = {"server_url", "profile", "workspace_id"}
    missing = sorted(required - multica.keys())
    if missing:
        raise _error(f"$.multica is missing: {', '.join(missing)}", "$.multica")
    result = {
        "server_url": _validate_origin(multica["server_url"], "$.multica.server_url"),
        "profile": _expect_string(multica["profile"], "$.multica.profile", _NAME),
        "workspace_id": _expect_string(
            multica["workspace_id"], "$.multica.workspace_id", _IDENTIFIER
        ),
    }
    has_source = "source_revision" in multica
    image_fields = [key for key in ("backend_image", "web_image") if key in multica]
    if has_source and image_fields:
        raise _error(
            "$.multica must contain either backend_image+web_image or source_revision",
            "$.multica",
        )
    if has_source:
        revision = _expect_string(multica["source_revision"], "$.multica.source_revision")
        if not _SOURCE_REVISION.fullmatch(revision):
            raise _error(
                "$.multica.source_revision must be a full 40- or 64-hex commit",
                "$.multica.source_revision",
            )
        result["source_revision"] = revision.lower()
        return result

    if set(image_fields) != {"backend_image", "web_image"}:
        raise _error(
            "$.multica must contain both backend_image and web_image digests",
            "$.multica",
        )
    for field in ("backend_image", "web_image"):
        image = _expect_string(multica[field], f"$.multica.{field}")
        if not _IMAGE_DIGEST.fullmatch(image):
            raise _error(
                f"$.multica.{field} must use an immutable @sha256:<64-hex> digest",
                f"$.multica.{field}",
            )
        result[field] = image
    return result


def _validate_agx(value: Any) -> dict[str, str]:
    agx = _expect_object(value, "$.agx")
    _check_fields(agx, _AGX_FIELDS, "$.agx")
    for key in _AGX_FIELDS:
        if key not in agx:
            raise _error(f"$.agx is missing: {key}", "$.agx")
    return {
        "version": _expect_string(agx["version"], "$.agx.version", _SEMVER),
        "installation_root": _validate_absolute_path(
            agx["installation_root"], "$.agx.installation_root"
        ),
    }


def _validate_nodes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _error("$.nodes must be an array", "$.nodes")
    if len(value) != 1:
        raise _error("v1 supports exactly one node", "$.nodes")
    result: list[dict[str, Any]] = []
    for index, raw_node in enumerate(value):
        path = f"$.nodes[{index}]"
        node = _expect_object(raw_node, path)
        _check_fields(node, _NODE_FIELDS, path)
        for key in _NODE_FIELDS:
            if key not in node:
                raise _error(f"{path} is missing: {key}", path)
        labels = node["labels"]
        if not isinstance(labels, list) or not labels:
            raise _error(f"{path}.labels must be a non-empty array", f"{path}.labels")
        normalized_labels = [
            _expect_string(label, f"{path}.labels[{label_index}]", _NAME)
            for label_index, label in enumerate(labels)
        ]
        if len(set(normalized_labels)) != len(normalized_labels):
            raise _error(f"{path}.labels must not contain duplicates", f"{path}.labels")
        platform = _expect_string(node["platform"], f"{path}.platform")
        if platform not in _PLATFORMS:
            raise _error(
                f"{path}.platform is not supported by fleet v1", f"{path}.platform"
            )
        result.append(
            {
                "name": _expect_string(node["name"], f"{path}.name", _NAME),
                "node_identity": _expect_string(
                    node["node_identity"], f"{path}.node_identity", _IDENTIFIER
                ),
                "platform": platform,
                "labels": normalized_labels,
            }
        )
    return result


def _validate_projects(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise _error("$.projects must be an array", "$.projects")
    if len(value) != 1:
        raise _error("$.projects must contain exactly one disposable project task", "$.projects")
    result: list[dict[str, str]] = []
    repository = re.compile(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z"
    )
    for index, raw_project in enumerate(value):
        path = f"$.projects[{index}]"
        project = _expect_object(raw_project, path)
        _check_fields(project, _PROJECT_FIELDS, path)
        for key in _PROJECT_FIELDS:
            if key not in project:
                raise _error(f"{path} is missing: {key}", path)
        result.append(
            {
                "name": _expect_string(project["name"], f"{path}.name", _NAME),
                "repository": _expect_string(
                    project["repository"], f"{path}.repository", repository
                ),
                "ref": _expect_string(project["ref"], f"{path}.ref", _REF),
                "environment": _expect_string(
                    project["environment"], f"{path}.environment", _NAME
                ),
            }
        )
    return result


def validate_contract(payload: Any) -> dict[str, Any]:
    """Validate and normalize a secret-free one-node v1 contract."""

    _scan_secret_fields(payload)
    root = _expect_object(payload, "$")
    _check_fields(root, _TOP_LEVEL_FIELDS, "$")
    version = root.get("contract_version")
    if isinstance(version, bool) or version != CONTRACT_VERSION:
        raise FleetPlanError(
            "$.contract_version must be the integer 1",
            path="$.contract_version",
            code="unsupported_contract_version",
        )
    for key in ("multica", "agx", "nodes", "projects"):
        if key not in root:
            raise _error(f"$ is missing: {key}", "$")
    return {
        "contract_version": CONTRACT_VERSION,
        "multica": _validate_multica(root["multica"]),
        "agx": _validate_agx(root["agx"]),
        "nodes": _validate_nodes(root["nodes"]),
        "projects": _validate_projects(root["projects"]),
    }


def load_contract(path: Path) -> dict[str, Any]:
    """Read a JSON contract without contacting any deployment endpoint."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FleetPlanError(
            f"contract file not found: {path}", path="--contract", code="contract_file"
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetPlanError(
            f"unable to read JSON contract: {path}",
            path="--contract",
            code="contract_file",
        ) from exc
    return validate_contract(payload)


def _required_tools(contract: Mapping[str, Any]) -> list[str]:
    tools = ["ssh", "scp"]
    if "source_revision" in contract["multica"]:
        tools.extend(("docker", "git"))
    return tools


def build_plan(
    contract: Mapping[str, Any],
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    """Build the stable external plan and validate local prerequisites."""

    tools = [
        {"name": name, "available": which(name) is not None}
        for name in _required_tools(contract)
    ]
    missing = [tool["name"] for tool in tools if not tool["available"]]
    if missing:
        raise FleetPlanError(
            "missing required local tool(s): " + ", ".join(missing),
            path="$.required_tools",
            code="missing_local_tool",
        )

    project = contract["projects"][0]
    identity_fields = (
        ("backend_image", "web_image")
        if "source_revision" not in contract["multica"]
        else ("source_revision",)
    )
    phases = [
        {
            "order": 1,
            "name": "multica",
            "action": "deploy or upgrade the declared Multica server",
        },
        {
            "order": 2,
            "name": "agx",
            "action": "install and configure the pinned AGX Bundle on the selected node",
        },
        {
            "order": 3,
            "name": "connector",
            "action": "connect the official Multica CLI to the declared profile and workspace",
        },
        {
            "order": 4,
            "name": "preflight",
            "action": "run the AGX–Multica connector preflight",
        },
    ]
    return {
        "status": "planned",
        "read_only": True,
        "contract_version": CONTRACT_VERSION,
        "multica": {
            "server_url": contract["multica"]["server_url"],
            "profile": contract["multica"]["profile"],
            "workspace_id": contract["multica"]["workspace_id"],
            **{
                field: contract["multica"][field]
                for field in identity_fields
            },
        },
        "agx": dict(contract["agx"]),
        "nodes": [dict(contract["nodes"][0])],
        "projects": [dict(project)],
        "disposable_task": {
            "project": project["name"],
            "repository": project["repository"],
            "ref": project["ref"],
            "environment": project["environment"],
        },
        "required_tools": tools,
        "apply_phases": phases,
    }


def _error_result(exc: FleetPlanError) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "invalid",
        "read_only": True,
        "error": {"code": exc.code, "message": str(exc)},
    }
    if exc.path:
        result["error"]["path"] = exc.path
    return result


def render(result: Mapping[str, Any], output_format: str = OUTPUT_HUMAN) -> str:
    """Render a plan/error with stable keys and line ordering."""

    if output_format == OUTPUT_JSON:
        return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if result.get("status") == "invalid":
        error = result["error"]
        location = f" ({error['path']})" if error.get("path") else ""
        return f"Fleet plan: invalid{location}\nerror: {error['message']}\n"
    lines = [
        "Fleet plan: planned",
        "read_only: true",
        f"contract_version: {result['contract_version']}",
        f"multica: {result['multica']['server_url']}",
        f"profile: {result['multica']['profile']}",
        f"workspace: {result['multica']['workspace_id']}",
    ]
    if "source_revision" in result["multica"]:
        lines.append(f"source_revision: {result['multica']['source_revision']}")
    else:
        lines.extend(
            [
                f"backend_image: {result['multica']['backend_image']}",
                f"web_image: {result['multica']['web_image']}",
            ]
        )
    lines.extend(
        [
            f"agx: {result['agx']['version']} at {result['agx']['installation_root']}",
            f"node: {result['nodes'][0]['name']} ({result['nodes'][0]['platform']})",
            f"project: {result['projects'][0]['name']} ({result['projects'][0]['environment']})",
            "disposable_task: true",
            "required_tools:",
        ]
    )
    lines.extend(
        f"  - {tool['name']}: {'available' if tool['available'] else 'missing'}"
        for tool in result["required_tools"]
    )
    lines.append("apply_phases:")
    lines.extend(
        f"  {phase['order']}. {phase['action']}" for phase in result["apply_phases"]
    )
    return "\n".join(lines) + "\n"


def run_plan(
    contract_path: Path,
    *,
    output_format: str = OUTPUT_HUMAN,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    """Run fleet plan and print exactly one stable result."""

    try:
        contract = load_contract(contract_path)
        result = build_plan(contract, which=which)
    except FleetPlanError as exc:
        print(render(_error_result(exc), output_format), end="")
        return 2
    print(render(result, output_format), end="")
    return 0
