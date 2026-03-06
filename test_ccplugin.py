#!/usr/bin/env python3
"""Tests for ccplugin. Run with: python -m pytest or python test_ccplugin.py"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Make ccplugin importable without executing main()
import importlib.util, types

spec = importlib.util.spec_from_file_location("ccplugin", Path(__file__).parent / "ccplugin.py")
ccplugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccplugin)


class TestWriteJson(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "data.json"
            ccplugin.write_json(p, {"a": 1})
            self.assertEqual(ccplugin.read_json(p), {"a": 1})

    def test_atomic_tmp_cleaned_up(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "data.json"
            ccplugin.write_json(p, {"x": 2})
            tmp = p.with_suffix(".tmp")
            self.assertFalse(tmp.exists(), ".tmp file should not remain after write")

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a" / "b" / "c.json"
            ccplugin.write_json(p, {})
            self.assertTrue(p.exists())

    def test_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.json"
            ccplugin.write_json(p, {"v": 1})
            ccplugin.write_json(p, {"v": 2})
            self.assertEqual(ccplugin.read_json(p)["v"], 2)


class TestReadJson(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(ccplugin.read_json(Path("/nonexistent/path.json")), {})

    def test_corrupt_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.json"
            p.write_text("not json", encoding="utf-8")
            self.assertEqual(ccplugin.read_json(p), {})


class TestIsGitUrl(unittest.TestCase):
    def test_https(self):
        self.assertTrue(ccplugin.is_git_url("https://github.com/foo/bar"))

    def test_git_at(self):
        self.assertTrue(ccplugin.is_git_url("git@github.com:foo/bar.git"))

    def test_dot_git_suffix(self):
        self.assertTrue(ccplugin.is_git_url("something.git"))

    def test_local_path(self):
        self.assertFalse(ccplugin.is_git_url("./local/path"))
        self.assertFalse(ccplugin.is_git_url("/absolute/path"))

    def test_bare_name(self):
        self.assertFalse(ccplugin.is_git_url("myplugin"))


class TestPluginNameValidation(unittest.TestCase):
    """resolve_plugin rejects names with path separators."""

    def _make_plugin_dir(self, tmp: Path, name: str) -> Path:
        plugin_dir = tmp / "repo"
        manifest_dir = plugin_dir / ".claude-plugin"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "plugin.json").write_text(
            json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
        )
        return plugin_dir

    def _resolve(self, plugin_dir: Path):
        """Call resolve_plugin against a local path."""
        with patch.object(ccplugin, "CACHE_DIR", plugin_dir.parent / "cache"):
            return ccplugin.resolve_plugin(str(plugin_dir))

    def test_valid_name_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            plugin_dir = self._make_plugin_dir(d, "my-plugin")
            result = self._resolve(plugin_dir)
            self.assertEqual(result["plugin_name"], "my-plugin")

    def test_slash_in_name_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            plugin_dir = self._make_plugin_dir(d, "../evil")
            with self.assertRaises(SystemExit):
                self._resolve(plugin_dir)

    def test_backslash_in_name_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            plugin_dir = self._make_plugin_dir(d, "..\\evil")
            with self.assertRaises(SystemExit):
                self._resolve(plugin_dir)

    def test_dotdot_name_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            plugin_dir = self._make_plugin_dir(d, "..")
            with self.assertRaises(SystemExit):
                self._resolve(plugin_dir)


class TestFindPluginsInDir(unittest.TestCase):
    def _make_plugin(self, base: Path, rel: str, name: str) -> Path:
        p = base / rel / ".claude-plugin"
        p.mkdir(parents=True)
        (p / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        return base / rel

    def test_finds_root_level_plugin(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self._make_plugin(d, "myplugin", "myplugin")
            found = ccplugin.find_plugins_in_dir(d)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].name, "myplugin")

    def test_finds_nested_plugin(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self._make_plugin(d, "group/nested-plugin", "nested-plugin")
            found = ccplugin.find_plugins_in_dir(d)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].name, "nested-plugin")

    def test_skips_node_modules(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self._make_plugin(d, "node_modules/bad", "bad")
            found = ccplugin.find_plugins_in_dir(d)
            self.assertEqual(found, [])

    def test_finds_multiple_plugins(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self._make_plugin(d, "alpha", "alpha")
            self._make_plugin(d, "beta", "beta")
            found = ccplugin.find_plugins_in_dir(d)
            self.assertEqual(len(found), 2)


class TestInstallUninstall(unittest.TestCase):
    """Integration-style tests that exercise install/uninstall against a tmp filesystem."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tmp = Path(self.tmp)

        # Patch all global paths to point into tmp
        self.patches = [
            patch.object(ccplugin, "CLAUDE_DIR", self.tmp / ".claude"),
            patch.object(ccplugin, "PLUGINS_DIR", self.tmp / ".claude" / "plugins"),
            patch.object(ccplugin, "INSTALLED_PLUGINS_FILE", self.tmp / ".claude" / "plugins" / "installed_plugins.json"),
            patch.object(ccplugin, "CACHE_DIR", self.tmp / ".claude" / "plugins" / "cache"),
            patch.object(ccplugin, "LOCAL_PLUGINS_DIR", self.tmp / ".claude" / "plugins" / "local"),
            patch.object(ccplugin, "USER_SETTINGS_FILE", self.tmp / ".claude" / "settings.json"),
            patch.object(ccplugin, "USER_LOCAL_SETTINGS_FILE", self.tmp / ".claude" / "settings.local.json"),
        ]
        for p in self.patches:
            p.start()

        # Create a fake plugin source
        self.plugin_src = self.tmp / "src" / "myplugin"
        plugin_manifest_dir = self.plugin_src / ".claude-plugin"
        plugin_manifest_dir.mkdir(parents=True)
        (plugin_manifest_dir / "plugin.json").write_text(
            json.dumps({"name": "myplugin", "version": "1.2.3", "description": "Test plugin"}),
            encoding="utf-8",
        )
        (self.plugin_src / "skills").mkdir()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmp)

    def test_install_creates_files(self):
        ccplugin.cmd_install(str(self.plugin_src), None, "user")

        install_path = ccplugin.LOCAL_PLUGINS_DIR / "myplugin"
        self.assertTrue(install_path.exists())
        self.assertTrue((install_path / ".claude-plugin" / "plugin.json").exists())

    def test_install_registers_in_json(self):
        ccplugin.cmd_install(str(self.plugin_src), None, "user")

        installed = ccplugin.read_json(ccplugin.INSTALLED_PLUGINS_FILE)
        self.assertIn("myplugin@local", installed["plugins"])
        record = installed["plugins"]["myplugin@local"][0]
        self.assertEqual(record["version"], "1.2.3")
        self.assertEqual(record["installedBy"], "ccplugin")

    def test_install_enables_in_settings(self):
        ccplugin.cmd_install(str(self.plugin_src), None, "user")

        settings = ccplugin.read_json(ccplugin.USER_SETTINGS_FILE)
        self.assertTrue(settings["enabledPlugins"].get("myplugin@local"))

    def test_uninstall_removes_files(self):
        ccplugin.cmd_install(str(self.plugin_src), None, "user")
        ccplugin.cmd_uninstall("myplugin", "user")

        install_path = ccplugin.LOCAL_PLUGINS_DIR / "myplugin"
        self.assertFalse(install_path.exists())

    def test_uninstall_removes_from_settings(self):
        ccplugin.cmd_install(str(self.plugin_src), None, "user")
        ccplugin.cmd_uninstall("myplugin", "user")

        settings = ccplugin.read_json(ccplugin.USER_SETTINGS_FILE)
        self.assertNotIn("myplugin@local", settings.get("enabledPlugins", {}))

    def test_reinstall_is_idempotent(self):
        ccplugin.cmd_install(str(self.plugin_src), None, "user")
        ccplugin.cmd_install(str(self.plugin_src), None, "user")

        installed = ccplugin.read_json(ccplugin.INSTALLED_PLUGINS_FILE)
        # Should not accumulate duplicate records
        self.assertEqual(len(installed["plugins"]["myplugin@local"]), 1)


if __name__ == "__main__":
    unittest.main()
