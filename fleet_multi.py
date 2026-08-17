"""Pure-domain multi-node fleet expansion.

The module accepts a secret-free desired-state mapping and returns a stable,
read-only expansion.  It deliberately has no transport, thread, subprocess,
Multica, or AGX client dependency.  AGX remains the fleet authority; receipts
and task links are retained as opaque, redacted references only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


MULTI_SCHEMA = "fleet-multi/v1"
STATUSES = ("planned", "configured", "healthy", "busy", "failed", "unavailable")

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SECRET_FIELD = re.compile(
    r"(?:secret|token|password|passwd|credential|authorization|cookie|"
    r"private[_-]?key|api[_-]?key|access[_-]?key|refresh[_-]?token|"
    r"ssh[_-]?(?:key|private))",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"(?i)(?:bearer\s+|(?:secret|token|password|passwd|credential|"
    r"authorization|cookie|private[_-]?key|api[_-]?key|access[_-]?key|"
    r"refresh[_-]?token)\s*[:=])\s*[^\s,};]+"
)


class FleetMultiError(ValueError):
    """A multi-node expansion cannot be produced safely."""

    def __init__(self, message: str, *, code: str = "invalid_config", path: str = ""):
        super().__init__(message)
        self.code = code
        self.path = path


def _fail(message: str, *, code: str = "invalid_config", path: str = "") -> None:
    raise FleetMultiError(message, code=code, path=path)


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{path} must be an object", path=path)
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{path} must be an array", path=path)
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{path} must be a non-empty string", path=path)
    if any(ord(char) < 32 for char in value):
        _fail(f"{path} contains a control character", path=path)
    return value


def _identity(value: Any, path: str) -> str:
    value = _text(value, path)
    if not _IDENTIFIER.fullmatch(value):
        _fail(f"{path} has an invalid stable identity", path=path)
    return value


def _check_fields(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    for key in value:
        if not isinstance(key, str):
            _fail(f"{path} contains a non-string field name", path=path)
        if key not in allowed:
            if _SECRET_FIELD.search(key):
                _fail(
                    f"{path}.{key} is not allowed in a secret-free fleet config",
                    code="sensitive_field",
                    path=f"{path}.{key}",
                )
            _fail(f"{path}.{key} is not a supported field", path=f"{path}.{key}")


def _alias(value: Mapping[str, Any], names: Sequence[str], path: str) -> Any:
    present = [name for name in names if name in value]
    if len(present) > 1:
        first = value[present[0]]
        if any(value[name] != first for name in present[1:]):
            _fail(f"{path} contains conflicting aliases", path=path)
    return value[present[0]] if present else None


def _required_identity(value: Mapping[str, Any], names: Sequence[str], path: str) -> str:
    raw = _alias(value, names, path)
    if raw is None:
        _fail(f"{path} is missing a stable identity", path=path)
    return _identity(raw, f"{path}.identity")


def _labels(value: Any, path: str) -> dict[str, str]:
    if isinstance(value, Mapping):
        result: dict[str, str] = {}
        for key, child in value.items():
            key_text = _text(key, f"{path} label name")
            if _SECRET_FIELD.search(key_text):
                _fail(
                    f"{path}.{key_text} is not allowed in a secret-free fleet config",
                    code="sensitive_field",
                    path=f"{path}.{key_text}",
                )
            result[key_text] = _text(child, f"{path}.{key_text}")
        if not result:
            _fail(f"{path} must not be empty", path=path)
        return result
    if isinstance(value, list):
        result = {}
        for index, child in enumerate(value):
            label = _text(child, f"{path}[{index}]")
            if label in result:
                _fail(f"{path} contains duplicate labels", path=path)
            result[label] = "true"
        if not result:
            _fail(f"{path} must not be empty", path=path)
        return result
    _fail(f"{path} must be an object or array", path=path)


def _redact_text(value: str) -> str:
    return _SECRET_TEXT.sub("<redacted>", value)


def redact(value: Any, *, key: str = "") -> Any:
    """Return a JSON-shaped value with secret-like content removed."""

    if key and _SECRET_FIELD.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(child_key): redact(child, key=str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [redact(child) for child in value]
    if isinstance(value, tuple):
        return [redact(child) for child in value]
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "<redacted>"


def _normalize_selector(value: Any, path: str) -> dict[str, Any]:
    if isinstance(value, str):
        return {"identity": _identity(value, path)}
    selector = _object(value, path)
    allowed = {"identity", "node_identity", "name", "labels"}
    _check_fields(selector, allowed, path)
    normalized: dict[str, Any] = {}
    identity = _alias(selector, ("identity", "node_identity"), path)
    if identity is not None:
        normalized["identity"] = _identity(identity, f"{path}.identity")
    if "name" in selector:
        normalized["name"] = _text(selector["name"], f"{path}.name")
    if "labels" in selector:
        normalized["labels"] = _labels(selector["labels"], f"{path}.labels")
    if not normalized:
        _fail(f"{path} must contain identity, name, or labels", path=path)
    return normalized


def _normalize_node(value: Any, index: int) -> dict[str, Any]:
    path = f"$.nodes[{index}]"
    node = _object(value, path)
    _check_fields(
        node,
        {
            "identity",
            "node_identity",
            "name",
            "labels",
            "workspace_id",
            "workspace",
            "profile",
            "status",
            "agx_receipt",
            "receipt",
            "agx_task_link",
            "task_link",
        },
        path,
    )
    identity = _required_identity(node, ("identity", "node_identity"), path)
    name = _text(node.get("name"), f"{path}.name")
    labels = _labels(node.get("labels"), f"{path}.labels")
    workspace = _alias(node, ("workspace_id", "workspace"), path)
    profile = _text(node["profile"], f"{path}.profile") if "profile" in node else None
    status = _text(node["status"], f"{path}.status") if "status" in node else "planned"
    if status not in STATUSES:
        _fail(f"{path}.status is not a supported fleet status", path=f"{path}.status")
    receipt = _alias(node, ("agx_receipt", "receipt"), path)
    if receipt is not None:
        _object(receipt, f"{path}.agx_receipt")
    task_link = _alias(node, ("agx_task_link", "task_link"), path)
    if task_link is not None:
        task_link = _text(task_link, f"{path}.agx_task_link")
    return {
        "identity": identity,
        "name": name,
        "labels": labels,
        "workspace_id": _text(workspace, f"{path}.workspace_id") if workspace is not None else None,
        "profile": profile,
        "status": status,
        "agx_receipt": redact(receipt) if receipt is not None else None,
        "agx_task_link": _redact_text(task_link) if task_link is not None else None,
    }


def _normalize_workspaces(value: Any) -> dict[str, dict[str, Any]]:
    workspaces = _array(value, "$.workspaces")
    if not workspaces:
        _fail("$.workspaces must not be empty", path="$.workspaces")
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(workspaces):
        path = f"$.workspaces[{index}]"
        workspace = _object(raw, path)
        _check_fields(workspace, {"identity", "workspace_id", "id", "name", "profile"}, path)
        identity = _required_identity(workspace, ("identity", "workspace_id", "id"), path)
        if identity in result:
            _fail(f"duplicate workspace identity: {identity}", code="duplicate_identity", path=path)
        result[identity] = {
            "identity": identity,
            "name": _text(workspace.get("name", identity), f"{path}.name"),
            "profile": _text(workspace.get("profile"), f"{path}.profile"),
        }
    return result


def _normalize_environments(value: Any, workspaces: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    environments = _array(value, "$.environments")
    if not environments:
        _fail("$.environments must not be empty", path="$.environments")
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(environments):
        path = f"$.environments[{index}]"
        environment = _object(raw, path)
        _check_fields(
            environment,
            {"identity", "environment_id", "id", "name", "workspace_id", "workspace", "profile"},
            path,
        )
        raw_identity = _alias(environment, ("identity", "environment_id", "id"), path)
        if raw_identity is None:
            raw_identity = environment.get("name")
        if raw_identity is None:
            _fail(f"{path} is missing a stable identity", path=path)
        identity = _identity(raw_identity, f"{path}.identity")
        if identity in result:
            _fail(f"duplicate environment identity: {identity}", code="duplicate_identity", path=path)
        workspace = _alias(environment, ("workspace_id", "workspace"), path)
        if workspace is None:
            _fail(f"{path} is missing workspace_id", path=path)
        workspace = _text(workspace, f"{path}.workspace_id")
        if workspace not in workspaces:
            _fail(f"unknown workspace: {workspace}", code="unknown_workspace", path=path)
        profile = _text(environment["profile"], f"{path}.profile") if "profile" in environment else workspaces[workspace]["profile"]
        if profile != workspaces[workspace]["profile"]:
            _fail(
                f"cross-profile environment/workspace mix: {identity}",
                code="cross_profile",
                path=path,
            )
        result[identity] = {
            "identity": identity,
            "name": _text(environment.get("name", identity), f"{path}.name"),
            "workspace_id": workspace,
            "profile": profile,
        }
    return result


def _normalize_projects(
    value: Any,
    environments: Mapping[str, Mapping[str, Any]],
    workspaces: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    projects = _array(value, "$.projects")
    if not projects:
        _fail("$.projects must not be empty", path="$.projects")
    result: list[dict[str, Any]] = []
    identities: set[str] = set()
    for index, raw in enumerate(projects):
        path = f"$.projects[{index}]"
        project = _object(raw, path)
        _check_fields(
            project,
            {
                "identity",
                "project_id",
                "id",
                "name",
                "environment",
                "environment_id",
                "workspace_id",
                "workspace",
                "profile",
                "selector",
                "node_selector",
                "selectors",
            },
            path,
        )
        raw_identity = _alias(project, ("identity", "project_id", "id"), path)
        if raw_identity is None:
            raw_identity = project.get("name")
        if raw_identity is None:
            _fail(f"{path} is missing a stable identity", path=path)
        identity = _identity(raw_identity, f"{path}.identity")
        if identity in identities:
            _fail(f"duplicate project identity: {identity}", code="duplicate_identity", path=path)
        identities.add(identity)
        environment = _alias(project, ("environment", "environment_id"), path)
        if environment is None:
            _fail(f"{path} is missing environment", path=path)
        environment = _text(environment, f"{path}.environment")
        if environment not in environments:
            _fail(f"unknown environment: {environment}", code="unknown_environment", path=path)
        expected_workspace = environments[environment]["workspace_id"]
        workspace = _alias(project, ("workspace_id", "workspace"), path)
        if workspace is not None:
            workspace = _text(workspace, f"{path}.workspace_id")
            if workspace not in workspaces:
                _fail(f"unknown workspace: {workspace}", code="unknown_workspace", path=path)
            if workspace != expected_workspace:
                _fail(
                    f"cross-workspace project/environment mix: {identity}",
                    code="cross_workspace",
                    path=path,
                )
        profile = _text(project["profile"], f"{path}.profile") if "profile" in project else environments[environment]["profile"]
        if profile != environments[environment]["profile"]:
            _fail(
                f"cross-profile project/environment mix: {identity}",
                code="cross_profile",
                path=path,
            )

        selectors: list[dict[str, Any]] = []
        if "selectors" in project:
            for selector_index, selector in enumerate(_array(project["selectors"], f"{path}.selectors")):
                selectors.append(_normalize_selector(selector, f"{path}.selectors[{selector_index}]"))
        selector = _alias(project, ("selector", "node_selector"), path)
        if selector is not None:
            selectors.append(_normalize_selector(selector, f"{path}.selector"))
        result.append(
            {
                "identity": identity,
                "name": _text(project.get("name", identity), f"{path}.name"),
                "environment": environment,
                "workspace_id": expected_workspace,
                "profile": profile,
                "selectors": selectors,
            }
        )
    return result


def _selector_matches(node: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    if "identity" in selector and node["identity"] != selector["identity"]:
        return False
    if "name" in selector and node["name"] != selector["name"]:
        return False
    if "labels" in selector:
        labels = selector["labels"]
        if any(node["labels"].get(key) != value for key, value in labels.items()):
            return False
    return True


def select_nodes(nodes: Sequence[Mapping[str, Any]], selector: Any) -> list[dict[str, Any]]:
    """Select exactly one node for one selector; ambiguity is fail-closed."""

    normalized_nodes = [_normalize_node(node, index) for index, node in enumerate(nodes)]
    identities = [node["identity"] for node in normalized_nodes]
    if len(set(identities)) != len(identities):
        _fail("duplicate node identity", code="duplicate_identity", path="$.nodes")
    normalized_selector = _normalize_selector(selector, "$.selector")
    matches = [node for node in normalized_nodes if _selector_matches(node, normalized_selector)]
    if not matches:
        _fail("no node matches selector", code="no_match", path="$.selector")
    if len(matches) > 1:
        _fail("selector matches multiple nodes", code="ambiguous_selector", path="$.selector")
    return matches


def _policy(value: Any, selected_count: int) -> dict[str, Any]:
    if value is None:
        return {"mode": "serial", "max_concurrency": 1, "executed": False}
    if isinstance(value, str):
        value = {"mode": value}
    policy = _object(value, "$.policy")
    _check_fields(policy, {"mode", "max_concurrency"}, "$.policy")
    mode = _text(policy.get("mode", "serial"), "$.policy.mode").lower()
    if mode not in {"serial", "parallel"}:
        _fail("$.policy.mode must be serial or parallel", path="$.policy.mode")
    raw_max = policy.get("max_concurrency", 1 if mode == "serial" else selected_count)
    if isinstance(raw_max, bool) or not isinstance(raw_max, int) or raw_max < 1:
        _fail("$.policy.max_concurrency must be a positive integer", path="$.policy.max_concurrency")
    if mode == "serial" and raw_max != 1:
        _fail("serial policy must use max_concurrency=1", path="$.policy.max_concurrency")
    if mode == "parallel" and raw_max < 2 and selected_count > 1:
        _fail("parallel policy must explicitly allow concurrency", path="$.policy.max_concurrency")
    return {"mode": mode, "max_concurrency": raw_max, "executed": False}


def _status_flags(status: str) -> dict[str, bool]:
    return {candidate: candidate == status or candidate == "planned" for candidate in STATUSES}


def _counts(nodes: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for node in nodes:
        counts[node["status"]] += 1
    return counts


def _overall_status(counts: Mapping[str, int]) -> str:
    for status in ("failed", "unavailable", "busy", "healthy", "configured", "planned"):
        if counts[status]:
            return status
    return "planned"


def build_multi_plan(config: Mapping[str, Any]) -> dict[str, Any]:
    """Expand a secret-free multi-node config without executing anything."""

    root = _object(config, "$")
    _check_fields(
        root,
        {
            "nodes",
            "projects",
            "environments",
            "workspaces",
            "selector",
            "selectors",
            "statuses",
            "node_statuses",
            "policy",
            "authority",
        },
        "$",
    )
    authority = root.get("authority", "agx")
    if not isinstance(authority, str) or authority.lower() != "agx":
        _fail(
            "Multica cannot be the fleet authority; AGX is authoritative",
            code="authority_violation",
            path="$.authority",
        )

    workspaces = _normalize_workspaces(root.get("workspaces"))
    environments = _normalize_environments(root.get("environments"), workspaces)
    raw_nodes = _array(root.get("nodes"), "$.nodes")
    if not raw_nodes:
        _fail("$.nodes must not be empty", path="$.nodes")
    nodes = [_normalize_node(node, index) for index, node in enumerate(raw_nodes)]
    identities = [node["identity"] for node in nodes]
    if len(set(identities)) != len(identities):
        duplicate = next(identity for identity in identities if identities.count(identity) > 1)
        _fail(
            f"duplicate node identity: {duplicate}",
            code="duplicate_identity",
            path="$.nodes",
        )
    projects = _normalize_projects(root.get("projects"), environments, workspaces)

    configured_statuses = root.get("statuses", root.get("node_statuses", {}))
    configured_statuses = _object(configured_statuses, "$.statuses")
    for identity, value in configured_statuses.items():
        if not isinstance(identity, str):
            _fail("$.statuses contains a non-string node identity", path="$.statuses")
        if _SECRET_FIELD.search(identity):
            _fail(
                f"$.statuses.{identity} is not allowed in a secret-free fleet config",
                code="sensitive_field",
                path=f"$.statuses.{identity}",
            )
        if identity not in identities:
            _fail(f"status references unknown node: {identity}", code="unknown_node", path="$.statuses")
        if isinstance(value, Mapping):
            _check_fields(value, {"status", "agx_receipt", "agx_task_link", "task_link"}, f"$.statuses.{identity}")
            status_value = value.get("status", "planned")
            receipt_value = value.get("agx_receipt")
            if receipt_value is not None:
                _object(receipt_value, f"$.statuses.{identity}.agx_receipt")
            link_value = value.get("agx_task_link", value.get("task_link"))
            if link_value is not None:
                _text(link_value, f"$.statuses.{identity}.agx_task_link")
        else:
            status_value = value
        status_value = _text(status_value, f"$.statuses.{identity}.status")
        if status_value not in STATUSES:
            _fail(f"unsupported status for node {identity}", path=f"$.statuses.{identity}")

    global_selectors: list[dict[str, Any]] = []
    if "selectors" in root:
        for index, selector in enumerate(_array(root["selectors"], "$.selectors")):
            global_selectors.append(_normalize_selector(selector, f"$.selectors[{index}]"))
    if "selector" in root:
        global_selectors.append(_normalize_selector(root["selector"], "$.selector"))

    assignments: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for project in projects:
        selectors = project["selectors"] or global_selectors
        if not selectors:
            selectors = [{"identity": node["identity"]} for node in nodes]
        for selector in selectors:
            matches = [node for node in nodes if _selector_matches(node, selector)]
            if not matches:
                _fail(
                    f"no node matches selector for project {project['identity']}",
                    code="no_match",
                    path=f"$.projects[{project['identity']}].selector",
                )
            if len(matches) > 1:
                _fail(
                    f"selector matches multiple nodes for project {project['identity']}",
                    code="ambiguous_selector",
                    path=f"$.projects[{project['identity']}].selector",
                )
            node = matches[0]
            if node["workspace_id"] is not None and node["workspace_id"] != project["workspace_id"]:
                _fail(
                    f"cross-workspace node/project mix: {node['identity']}",
                    code="cross_workspace",
                    path="$.nodes",
                )
            if node["profile"] is not None and node["profile"] != project["profile"]:
                _fail(
                    f"cross-profile node/project mix: {node['identity']}",
                    code="cross_profile",
                    path="$.nodes",
                )
            assignments.append((node, project, environments[project["environment"]]))

    scopes = {(project["workspace_id"], project["profile"]) for _, project, _ in assignments}
    if len({scope[0] for scope in scopes}) > 1:
        _fail("cross-workspace fleet mixing is not allowed", code="cross_workspace", path="$.projects")
    if len({scope[1] for scope in scopes}) > 1:
        _fail("cross-profile fleet mixing is not allowed", code="cross_profile", path="$.projects")

    status_records: dict[str, dict[str, Any]] = {}
    for node in nodes:
        status_records[node["identity"]] = {
            "status": node["status"],
            "agx_receipt": node["agx_receipt"],
            "agx_task_link": node["agx_task_link"],
        }
    for identity, value in configured_statuses.items():
        if isinstance(value, Mapping):
            receipt_value = value.get("agx_receipt", status_records[identity]["agx_receipt"])
            link_value = value.get(
                "agx_task_link",
                value.get("task_link", status_records[identity]["agx_task_link"]),
            )
            status_records[identity].update(
                {
                    "status": value.get("status", status_records[identity]["status"]),
                    "agx_receipt": redact(receipt_value),
                    "agx_task_link": _redact_text(link_value) if link_value is not None else None,
                }
            )
        else:
            status_records[identity]["status"] = value

    selected: dict[str, dict[str, Any]] = {}
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for node, project, environment in assignments:
        record = status_records[node["identity"]]
        rendered_node = selected.setdefault(
            node["identity"],
            {
                "identity": node["identity"],
                "name": node["name"],
                "labels": dict(node["labels"]),
                "workspace_id": project["workspace_id"],
                "profile": project["profile"],
                "environment": project["environment"],
                "status": record["status"],
                "status_flags": _status_flags(record["status"]),
                "agx_receipt": record["agx_receipt"],
                "agx_task_link": record["agx_task_link"],
            },
        )
        group_key = (project["workspace_id"], project["profile"], project["environment"])
        group = groups.setdefault(
            group_key,
            {
                "workspace_id": project["workspace_id"],
                "profile": project["profile"],
                "environment": project["environment"],
                "projects": {},
                "nodes": {},
            },
        )
        group["projects"][project["identity"]] = {
            "identity": project["identity"],
            "name": project["name"],
        }
        group["nodes"][node["identity"]] = rendered_node

    rendered_nodes = [selected[identity] for identity in sorted(selected)]
    rendered_groups = []
    for key in sorted(groups):
        group = groups[key]
        group_nodes = [group["nodes"][identity] for identity in sorted(group["nodes"])]
        group["projects"] = [group["projects"][identity] for identity in sorted(group["projects"])]
        group["nodes"] = group_nodes
        group["status_counts"] = _counts(group_nodes)
        group["status"] = _overall_status(group["status_counts"])
        rendered_groups.append(group)

    status_counts = _counts(rendered_nodes)
    return {
        "schema": MULTI_SCHEMA,
        "status": _overall_status(status_counts),
        "read_only": True,
        "authority": "agx",
        "execution_policy": _policy(root.get("policy"), len(rendered_nodes)),
        "live_gate": "not_run",
        "nodes": rendered_nodes,
        "groups": rendered_groups,
        "status_counts": status_counts,
    }


__all__ = [
    "FleetMultiError",
    "MULTI_SCHEMA",
    "STATUSES",
    "build_multi_plan",
    "redact",
    "select_nodes",
]
