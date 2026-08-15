import json
import tempfile
import unittest
from pathlib import Path

import agent_plugins_to_multica as converter


class AgentPluginsToMulticaTest(unittest.TestCase):
    def make_source(self, root: Path) -> Path:
        source = root / "sample-plugin"
        (source / ".codex-plugin").mkdir(parents=True)
        (source / "skills" / "triage").mkdir(parents=True)
        (source / "skills" / "triage" / "references").mkdir()
        (source / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "Sample Plugin",
                    "version": "1.2.3",
                    "description": "A static test plugin.",
                }
            ),
            encoding="utf-8",
        )
        (source / "skills" / "triage" / "SKILL.md").write_text(
            "---\nname: Triage\ndescription: >-\n  Collect facts before proposing a fix.\n---\n\n# Triage\n",
            encoding="utf-8",
        )
        (source / "skills" / "triage" / "references" / "checklist.md").write_text(
            "facts first\n", encoding="utf-8"
        )
        # Provider metadata and root scripts must not be copied into Multica.
        (source / "scripts").mkdir()
        (source / "scripts" / "ignored.py").write_text("print('no')\n", encoding="utf-8")
        return source

    def test_conversion_emits_manifest_and_deterministic_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_source(root)
            first = converter.convert(source, root / "out-1", root / "first.zip", "dev.agent-plugins", "zaurakworks")
            second = converter.convert(source, root / "out-2", root / "second.zip", "dev.agent-plugins", "zaurakworks")

            self.assertEqual(first["archive_sha256"], second["archive_sha256"])
            manifest = json.loads((root / "out-1" / "multica.plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["metadata"]["key"], "dev.agent-plugins.sample-plugin")
            self.assertEqual(manifest["contributes"]["agent_skills"][0]["key"], "triage")
            self.assertTrue((root / "out-1" / "skills" / "triage" / "references" / "checklist.md").exists())
            self.assertFalse((root / "out-1" / "scripts").exists())

    def test_conversion_rejects_missing_skill_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_source(root)
            (source / "skills" / "triage" / "SKILL.md").unlink()
            with self.assertRaises(converter.ConversionError):
                converter.convert(source, root / "out", None, "dev.agent-plugins", "zaurakworks")


if __name__ == "__main__":
    unittest.main()
