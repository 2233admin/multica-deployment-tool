#!/usr/bin/env python3
"""Convert an agent-plugins plugin into a Multica V1 private Skill Plugin.

The source repository deliberately contains provider-specific metadata,
scripts, tests, and other files that Multica's V1 private-plugin contract does
not accept.  This adapter copies only static UTF-8 files below ``skills/`` and
emits the strict ``multica.plugin.json`` manifest expected by Multica.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


API_VERSION = "multica.plugin/v1"
KIND_PLUGIN = "Plugin"
HOST_API = ">=1.0.0 <2.0.0"
REQUIRED_DAEMON_FEATURES = ["execution-manifest-v1", "agent-skill-v1"]
REQUESTED_CAPABILITIES = ["agent.skill.contribute"]
MAX_FILE_SIZE = 1 << 20
MAX_SKILL_SIZE = 8 << 20
MAX_ARTIFACT_SIZE = 32 << 20
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
PUBLISHER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
HOOK_NAMES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "install",
    "install.sh",
    "install.ps1",
    "install.cmd",
    "install.bat",
    "preinstall",
    "preinstall.sh",
    "preinstall.ps1",
    "preinstall.cmd",
    "preinstall.bat",
    "postinstall",
    "postinstall.sh",
    "postinstall.ps1",
    "postinstall.cmd",
    "postinstall.bat",
    "prepare",
    "prepare.sh",
    "prepare.ps1",
    "prepare.cmd",
    "prepare.bat",
    "build",
    "build.sh",
    "build.ps1",
    "build.cmd",
    "build.bat",
}


class ConversionError(ValueError):
    """A source plugin cannot be represented safely by Multica V1."""


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError(f"无法读取插件元数据 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConversionError(f"插件元数据必须是 JSON 对象: {path}")
    return value


def slug(value: str, fallback: str = "skill") -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not result or not result[0].isalpha():
        result = f"{fallback}-{result}" if result else fallback
    return result


def one_line(value: str, fallback: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value or fallback


def parse_frontmatter(path: Path, default_name: str, default_description: str) -> Tuple[str, str]:
    """Read the small YAML frontmatter subset used by agent-plugins Skills."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConversionError(f"Skill 不是 UTF-8 文本: {path}: {exc}") from exc

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return one_line(default_name, default_name), one_line(default_description, default_description)
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise ConversionError(f"Skill frontmatter 没有结束标记: {path}")

    fields: Dict[str, List[str]] = {}
    current = ""
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s*(.*))?$", line)
        if match:
            current = match.group(1).lower()
            fields[current] = [match.group(2) or ""]
            continue
        if current and (line.startswith(" ") or line.startswith("\t")):
            fields[current].append(line.strip())

    name = one_line(" ".join(fields.get("name", [])), default_name)
    description_raw = " ".join(fields.get("description", []))
    description_raw = re.sub(r"^(?:>-?|\|-?)\s*", "", description_raw)
    description = one_line(description_raw, default_description)
    return name, description


def load_source_metadata(source: Path) -> Tuple[str, str, str]:
    candidates = [source / ".codex-plugin" / "plugin.json", source / ".claude-plugin" / "plugin.json"]
    metadata_path = next((path for path in candidates if path.is_file()), None)
    if metadata_path is None:
        raise ConversionError("找不到 .codex-plugin/plugin.json 或 .claude-plugin/plugin.json")
    metadata = read_json(metadata_path)
    name = one_line(str(metadata.get("name", "")), source.name)
    version = one_line(str(metadata.get("version", "")), "")
    if not SEMVER_RE.fullmatch(version):
        raise ConversionError(f"插件版本不是 SemVer: {version!r}")
    description = one_line(str(metadata.get("description", "")), f"来自 agent-plugins 的 {name} Skill Plugin。")
    return name, version, description


def collect_skill_files(source: Path) -> List[Tuple[str, bytes]]:
    skills_root = source / "skills"
    if not skills_root.is_dir():
        raise ConversionError(f"插件没有 skills/ 目录: {source}")

    skill_dirs = sorted((path for path in skills_root.iterdir() if path.is_dir()), key=lambda item: item.name.lower())
    if not skill_dirs:
        raise ConversionError(f"插件没有可转换的 Skill: {skills_root}")

    files: List[Tuple[str, bytes]] = []
    seen_keys: set[str] = set()
    total_size = 0
    for skill_dir in skill_dirs:
        skill_key = slug(skill_dir.name)
        if not KEY_RE.fullmatch(skill_key) or skill_key.startswith("multica-"):
            raise ConversionError(f"无法生成安全的 Skill key: {skill_dir.name}")
        if skill_key in seen_keys:
            raise ConversionError(f"Skill key 冲突: {skill_key}")
        seen_keys.add(skill_key)

        entry = skill_dir / "SKILL.md"
        if not entry.is_file():
            raise ConversionError(f"Skill 缺少 SKILL.md: {skill_dir}")
        for path in sorted((candidate for candidate in skill_dir.rglob("*") if candidate.is_file()), key=lambda item: item.as_posix().lower()):
            if path.is_symlink():
                raise ConversionError(f"Skill 包含符号链接，拒绝转换: {path}")
            relative = path.relative_to(skill_dir).as_posix()
            if Path(relative).name.lower() in HOOK_NAMES:
                raise ConversionError(f"Skill 文件名被 Multica V1 拒绝为安装/构建 Hook: {relative}")
            try:
                content = path.read_bytes()
                content.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ConversionError(f"Skill 文件必须是 UTF-8 文本: {path}: {exc}") from exc
            if len(content) > MAX_FILE_SIZE:
                raise ConversionError(f"Skill 文件超过 {MAX_FILE_SIZE} 字节: {path}")
            total_size += len(content)
            if total_size > MAX_ARTIFACT_SIZE:
                raise ConversionError(f"插件总内容超过 {MAX_ARTIFACT_SIZE} 字节")
            skill_total = sum(size for target, size in ((name, len(data)) for name, data in files) if target.startswith(f"skills/{skill_key}/")) + len(content)
            if skill_total > MAX_SKILL_SIZE:
                raise ConversionError(f"Skill {skill_key} 内容超过 {MAX_SKILL_SIZE} 字节")
            target = f"skills/{skill_key}/{relative}"
            files.append((target, content))
    return files


def build_manifest(source: Path, key_prefix: str, publisher: str, files: List[Tuple[str, bytes]]) -> dict:
    plugin_name, version, description = load_source_metadata(source)
    contributions = []
    skill_keys = sorted({name.split("/", 2)[1] for name, _ in files})
    for skill_key in skill_keys:
        entry_name = next(name for name, _ in files if name == f"skills/{skill_key}/SKILL.md")
        entry_source = source / "skills" / skill_key
        # The source directory can use a non-normalized name; locate by the
        # generated key rather than assuming it was already slugged.
        source_skill_dir = next(path for path in (source / "skills").iterdir() if path.is_dir() and slug(path.name) == skill_key)
        skill_name, skill_description = parse_frontmatter(entry_source if entry_source.is_file() else source_skill_dir / "SKILL.md", skill_key, description)
        contributions.append({
            "key": skill_key,
            "name": one_line(skill_name, skill_key),
            "description": one_line(skill_description, description),
            "entry": entry_name,
        })

    plugin_key = f"{key_prefix.rstrip('.')}.{slug(source.name)}"
    return {
        "api_version": API_VERSION,
        "kind": KIND_PLUGIN,
        "metadata": {
            "key": plugin_key,
            "name": plugin_name,
            "description": description,
            "version": version,
            "publisher": publisher,
        },
        "compatibility": {
            "host_api": HOST_API,
            "required_daemon_features": REQUIRED_DAEMON_FEATURES,
        },
        "requested_capabilities": REQUESTED_CAPABILITIES,
        "contributes": {"agent_skills": contributions},
    }


def validate_manifest(manifest: dict) -> None:
    """Fail locally with the same important limits as Multica V1."""

    metadata = manifest["metadata"]
    plugin_key = metadata["key"]
    if len(plugin_key.encode("utf-8")) > 255 or any(not KEY_RE.fullmatch(segment) for segment in plugin_key.split(".")):
        raise ConversionError(f"生成的 Plugin key 不符合 reverse-DNS 规则: {plugin_key}")
    if not 0 < len(metadata["name"].encode("utf-8")) <= 160:
        raise ConversionError("Plugin name 必须是 160 字节以内的单行文本")
    if len(metadata["description"].encode("utf-8")) > 2000:
        raise ConversionError("Plugin description 超过 2000 字节")
    if not PUBLISHER_RE.fullmatch(metadata["publisher"]):
        raise ConversionError(f"publisher 不符合 Multica 规则: {metadata['publisher']!r}")

    names: set[str] = set()
    for contribution in manifest["contributes"]["agent_skills"]:
        if not KEY_RE.fullmatch(contribution["key"]) or contribution["key"].startswith("multica-"):
            raise ConversionError(f"Skill key 不符合 Multica 规则: {contribution['key']}")
        name = contribution["name"]
        if not 0 < len(name.encode("utf-8")) <= 160 or "\n" in name or "\r" in name:
            raise ConversionError(f"Skill name 不符合 Multica 规则: {name!r}")
        if len(contribution["description"].encode("utf-8")) > 2000:
            raise ConversionError(f"Skill description 超过 2000 字节: {contribution['key']}")
        lowered = name.lower()
        if lowered in names:
            raise ConversionError(f"Skill name 重复: {name}")
        names.add(lowered)


def write_archive(root: Path, output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((candidate for candidate in root.rglob("*") if candidate.is_file()), key=lambda item: item.relative_to(root).as_posix()):
            name = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100644 & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())
    return hashlib.sha256(output.read_bytes()).hexdigest()


def convert(source: Path, output_dir: Path, archive: Path | None, key_prefix: str, publisher: str) -> dict:
    source = source.resolve()
    if not source.is_dir():
        raise ConversionError(f"源插件目录不存在: {source}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ConversionError(f"输出目录不是空目录: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    files = collect_skill_files(source)
    manifest = build_manifest(source, key_prefix, publisher, files)
    validate_manifest(manifest)
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (output_dir / "multica.plugin.json").write_bytes(manifest_bytes)
    for target, content in files:
        path = output_dir / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    archive_digest = None
    if archive is not None:
        archive_digest = write_archive(output_dir, archive.resolve())
    return {
        "plugin_key": manifest["metadata"]["key"],
        "version": manifest["metadata"]["version"],
        "skills": [entry["key"] for entry in manifest["contributes"]["agent_skills"]],
        "output_dir": str(output_dir),
        "archive": str(archive.resolve()) if archive is not None else None,
        "archive_sha256": archive_digest,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 agent-plugins 的静态 Skill 转成 Multica Private Plugin V1")
    parser.add_argument("plugin_dir", type=Path, help="agent-plugins/plugins/<plugin> 目录")
    parser.add_argument("--output-dir", type=Path, required=True, help="Multica Plugin 源目录（必须为空）")
    parser.add_argument("--archive", type=Path, help="同时生成可上传的 ZIP")
    parser.add_argument("--key-prefix", default="dev.agent-plugins", help="Plugin key 前缀（默认：dev.agent-plugins）")
    parser.add_argument("--publisher", default="zaurakworks", help="Multica publisher（默认：zaurakworks）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = convert(args.plugin_dir, args.output_dir, args.archive, args.key_prefix, args.publisher)
    except (ConversionError, OSError, StopIteration) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Converted {result['plugin_key']} {result['version']}")
        print(f"Output: {result['output_dir']}")
        if result["archive"]:
            print(f"Archive: {result['archive']}")
            print(f"SHA-256: {result['archive_sha256']}")
        print(f"Skills: {', '.join(result['skills'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
