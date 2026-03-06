#!/usr/bin/env python3
"""
ccplugin - Local Claude Code Plugin Manager

Installs plugins from any Git repo or local path as local plugins,
bypassing marketplace allowlist restrictions.

Usage:
  ccplugin install <source> [plugin-name] [--scope user|project|local]
  ccplugin uninstall <plugin-name> [--scope user|project|local]
  ccplugin list
  ccplugin update <plugin-name>
  ccplugin info <plugin-name>

Examples:
  ccplugin install https://github.com/obra/superpowers
  ccplugin install https://github.com/anthropics/claude-code feature-dev --scope project
  ccplugin install ./local-path/my-plugin
  ccplugin uninstall superpowers
  ccplugin list
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl as _fcntl

    @contextlib.contextmanager
    def _lock(path: Path):
        """Exclusive advisory lock on a lockfile (POSIX)."""
        lock_path = path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as fh:
            _fcntl.flock(fh, _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(fh, _fcntl.LOCK_UN)

except ImportError:  # Windows — no fcntl
    @contextlib.contextmanager
    def _lock(path: Path):
        yield

# ── Paths ───────────────────────────────────────────────────────────────────

CLAUDE_DIR = Path.home() / ".claude"
PLUGINS_DIR = CLAUDE_DIR / "plugins"
INSTALLED_PLUGINS_FILE = PLUGINS_DIR / "installed_plugins.json"
CACHE_DIR = PLUGINS_DIR / "cache"
LOCAL_PLUGINS_DIR = PLUGINS_DIR / "local"
USER_SETTINGS_FILE = CLAUDE_DIR / "settings.json"
USER_LOCAL_SETTINGS_FILE = CLAUDE_DIR / "settings.local.json"

# Managed settings locations (set by IT/admin)

import platform as _platform

_sys = _platform.system()
if _sys == "Darwin":
    MANAGED_SETTINGS_FILE = Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    MANAGED_MCP_FILE = Path("/Library/Application Support/ClaudeCode/managed-mcp.json")
elif _sys == "Windows":
    MANAGED_SETTINGS_FILE = Path(r"C:\Program Files\ClaudeCode\managed-settings.json")
    MANAGED_MCP_FILE = Path(r"C:\Program Files\ClaudeCode\managed-mcp.json")
else:  # Linux / WSL
    MANAGED_SETTINGS_FILE = Path("/etc/claude-code/managed-settings.json")
    MANAGED_MCP_FILE = Path("/etc/claude-code/managed-mcp.json")

# ── Helpers ─────────────────────────────────────────────────────────────────

class Colors:
    """ANSI color codes, disabled when not a TTY."""

    _enabled = sys.stdout.isatty()

    GREEN = "\033[32m" if _enabled else ""
    RED = "\033[31m" if _enabled else ""
    YELLOW = "\033[33m" if _enabled else ""
    CYAN = "\033[36m" if _enabled else ""
    BOLD = "\033[1m" if _enabled else ""
    DIM = "\033[2m" if _enabled else ""
    RESET = "\033[0m" if _enabled else ""


def log(msg: str) -> None:
    print(f"  {msg}")


def success(msg: str) -> None:
    print(f"\n  {Colors.GREEN}✓{Colors.RESET} {msg}\n")


def error(msg: str) -> None:
    print(f"\n  {Colors.RED}✗{Colors.RESET} {msg}\n", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"  {Colors.YELLOW}⚠{Colors.RESET} {msg}")


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX; best-effort on Windows


def short_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def is_git_url(source: str) -> bool:
    return any(
        source.startswith(prefix)
        for prefix in ("https://", "git@", "ssh://", "http://")
    ) or source.endswith(".git")


def run_git(args: list[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Run a git command, raising on failure."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def get_project_dir() -> Path:
    return Path.cwd()


def get_settings_file(scope: str) -> Path:
    if scope == "user":
        return USER_SETTINGS_FILE
    elif scope == "local":
        return get_project_dir() / ".claude" / "settings.local.json"
    elif scope == "project":
        return get_project_dir() / ".claude" / "settings.json"
    return USER_SETTINGS_FILE

# ── Plugin Discovery ───────────────────────────────────────────────────────

def find_plugins_in_dir(directory: Path) -> list[Path]:
    """Search up to 2 levels deep for directories containing .claude-plugin/plugin.json."""
    results = []
    if not directory.is_dir():
        return results

    for entry in sorted(directory.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name == "node_modules":
            continue

        manifest = entry / ".claude-plugin" / "plugin.json"
        if manifest.exists():
            results.append(entry)

        # One level deeper (marketplace layout: marketplace/plugin-name/)
        try:
            for sub in sorted(entry.iterdir()):
                if not sub.is_dir() or sub.name.startswith("."):
                    continue
                if (sub / ".claude-plugin" / "plugin.json").exists():
                    results.append(sub)
        except OSError:
            pass

    return results


def resolve_plugin(
    source: str, target_name: Optional[str] = None
) -> dict[str, Any]:
    """
    Resolve a plugin source to a local directory.

    Returns dict with keys: plugin_dir, plugin_name, plugin_meta, git_commit_sha
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    source_dir: Path
    git_commit_sha: Optional[str] = None

    if is_git_url(source):
        repo_hash = short_hash(source)
        clone_target = CACHE_DIR / f"repo-{repo_hash}"

        if clone_target.exists():
            log("Updating cached repo...")
            try:
                # shallow clones can't do pull --ff-only; fetch+reset works reliably
                run_git(["fetch", "--depth", "1", "origin"], cwd=clone_target)
                # origin/HEAD may not be set on all remotes; fall back to common branch names
                for ref in ("origin/HEAD", "origin/main", "origin/master"):
                    try:
                        run_git(["reset", "--hard", ref], cwd=clone_target)
                        break
                    except subprocess.CalledProcessError:
                        continue
                else:
                    raise subprocess.CalledProcessError(1, "reset", "no resolvable ref")
            except subprocess.CalledProcessError:
                warn("Fetch failed, re-cloning...")
                shutil.rmtree(clone_target, ignore_errors=True)
                log(f"Cloning {source}...")
                run_git(["clone", "--depth", "1", source, str(clone_target)])
        else:
            log(f"Cloning {source}...")
            try:
                run_git(["clone", "--depth", "1", source, str(clone_target)])
            except subprocess.CalledProcessError as e:
                error(f"Git clone failed: {e.stderr.strip()}")

        try:
            result = run_git(["rev-parse", "HEAD"], cwd=clone_target)
            git_commit_sha = result.stdout.strip()
        except subprocess.CalledProcessError:
            pass

        source_dir = clone_target
    else:
        source_dir = Path(source).resolve()
        if not source_dir.exists():
            error(f"Source path does not exist: {source_dir}")

    # Find the plugin within the source directory
    plugin_dir: Optional[Path] = None
    plugin_meta: dict = {}

    # Case 1: Root is the plugin
    root_manifest = source_dir / ".claude-plugin" / "plugin.json"
    if root_manifest.exists():
        plugin_meta = read_json(root_manifest)
        if not target_name or plugin_meta.get("name") == target_name:
            plugin_dir = source_dir

    # Case 2: Search subdirectories for target plugin
    if plugin_dir is None and target_name:
        for candidate in find_plugins_in_dir(source_dir):
            meta = read_json(candidate / ".claude-plugin" / "plugin.json")
            if meta.get("name") == target_name or candidate.name == target_name:
                plugin_dir = candidate
                plugin_meta = meta
                break

    # Case 3: No target specified, auto-detect
    if plugin_dir is None and not target_name:
        candidates = find_plugins_in_dir(source_dir)
        if len(candidates) == 1:
            plugin_dir = candidates[0]
            plugin_meta = read_json(plugin_dir / ".claude-plugin" / "plugin.json")
        elif len(candidates) > 1:
            print("\n  Multiple plugins found in source. Specify one:\n")
            for c in candidates:
                meta = read_json(c / ".claude-plugin" / "plugin.json")
                name = meta.get("name", c.name)
                desc = meta.get("description", "")
                suffix = f"  {Colors.DIM}{desc}{Colors.RESET}" if desc else ""
                print(f"    - {Colors.BOLD}{name}{Colors.RESET}{suffix}")
            print(f"\n  Usage: ccplugin install {source} <plugin-name>\n")
            sys.exit(1)

    if plugin_dir is None:
        if target_name:
            error(f'Plugin "{target_name}" not found in {source}')
        else:
            error(f"No plugin found in {source}. Does it have .claude-plugin/plugin.json?")

    name = plugin_meta.get("name") or target_name or plugin_dir.name

    # Guard against path traversal (e.g. "../../../etc/passwd")
    if "/" in name or "\\" in name or name in (".", ".."):
        error(f'Invalid plugin name "{name}": must not contain path separators')

    return {
        "plugin_dir": plugin_dir,
        "plugin_name": name,
        "plugin_meta": plugin_meta,
        "git_commit_sha": git_commit_sha,
    }

# ── Commands ────────────────────────────────────────────────────────────────

def cmd_install(source: str, target_name: Optional[str], scope: str) -> None:
    print(f"\n  {Colors.BOLD}ccplugin install{Colors.RESET}\n")

    resolved = resolve_plugin(source, target_name)
    plugin_dir: Path = resolved["plugin_dir"]
    plugin_name: str = resolved["plugin_name"]
    plugin_meta: dict = resolved["plugin_meta"]
    git_commit_sha: Optional[str] = resolved["git_commit_sha"]

    version = plugin_meta.get("version", "unknown")
    log(f"Found plugin: {Colors.CYAN}{plugin_name}{Colors.RESET} (v{version})")
    if desc := plugin_meta.get("description"):
        log(f"  {Colors.DIM}{desc}{Colors.RESET}")

    # Copy to local plugins directory (atomic: copy to tmp, then replace)
    install_path = LOCAL_PLUGINS_DIR / plugin_name
    LOCAL_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = LOCAL_PLUGINS_DIR / f".tmp-{plugin_name}"

    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    try:
        shutil.copytree(plugin_dir, tmp_path)
    except Exception as e:
        shutil.rmtree(tmp_path, ignore_errors=True)
        error(f"Failed to copy plugin files: {e}")

    if install_path.exists():
        shutil.rmtree(install_path)
    tmp_path.rename(install_path)
    log(f"Installed to {install_path}")

    # Register in installed_plugins.json and enable in settings (locked)
    plugin_key = f"{plugin_name}@local"
    record: dict[str, Any] = {
        "scope": scope,
        "isLocal": True,
        "installPath": str(install_path),
        "version": plugin_meta.get("version", "0.0.0"),
        "source": source if is_git_url(source) else str(Path(source).resolve()),
        "targetName": target_name,
        "installedAt": datetime.now(timezone.utc).isoformat(),
        "installedBy": "ccplugin",
    }
    if scope != "user":
        record["projectPath"] = str(get_project_dir())
    if git_commit_sha:
        record["gitCommitSha"] = git_commit_sha

    with _lock(INSTALLED_PLUGINS_FILE):
        installed = read_json(INSTALLED_PLUGINS_FILE)
        installed.setdefault("plugins", {})

        # Store as array (matches Claude Code's internal format)
        records = installed["plugins"].get(plugin_key, [])
        if not isinstance(records, list):
            records = []

        # Remove existing entry for same scope/project
        records = [
            r
            for r in records
            if not (r.get("scope") == scope and r.get("projectPath") == record.get("projectPath"))
        ]
        records.append(record)
        installed["plugins"][plugin_key] = records
        write_json(INSTALLED_PLUGINS_FILE, installed)

    log("Registered in installed_plugins.json")

    settings_file = get_settings_file(scope)
    with _lock(settings_file):
        settings = read_json(settings_file)
        settings.setdefault("enabledPlugins", {})
        settings["enabledPlugins"][plugin_key] = True
        write_json(settings_file, settings)

    log(f"Enabled in {settings_file}")

    # Workaround: local scope requires enabledPlugins key in main settings.json
    if scope == "local":
        with _lock(USER_SETTINGS_FILE):
            main_settings = read_json(USER_SETTINGS_FILE)
            if "enabledPlugins" not in main_settings:
                main_settings["enabledPlugins"] = {}
                write_json(USER_SETTINGS_FILE, main_settings)
                warn(f"Created enabledPlugins key in {USER_SETTINGS_FILE} (required for local scope merge)")

    success(f"{plugin_name} installed (scope: {scope}). Restart Claude Code to load.")


def cmd_uninstall(plugin_name: str, scope: str) -> None:
    print(f"\n  {Colors.BOLD}ccplugin uninstall{Colors.RESET}\n")

    plugin_key = plugin_name if "@" in plugin_name else f"{plugin_name}@local"
    base_name = plugin_name.split("@")[0]

    # Remove from installed_plugins.json (locked)
    removed_path: Optional[str] = None
    with _lock(INSTALLED_PLUGINS_FILE):
        installed = read_json(INSTALLED_PLUGINS_FILE)
        records = installed.get("plugins", {}).get(plugin_key, [])

        if records:
            project_dir = str(get_project_dir())
            matching = [
                r
                for r in records
                if r.get("scope") == scope
                and (scope == "user" or r.get("projectPath") == project_dir)
            ]

            if matching:
                removed_path = matching[0].get("installPath")
                for m in matching:
                    records.remove(m)

                if records:
                    installed["plugins"][plugin_key] = records
                else:
                    installed["plugins"].pop(plugin_key, None)

                write_json(INSTALLED_PLUGINS_FILE, installed)
                log("Removed from installed_plugins.json")
            else:
                warn(f"No {scope}-scoped installation found for {plugin_key}")
        else:
            warn(f"{plugin_key} not found in installed_plugins.json")

    # Remove from settings (locked)
    settings_file = get_settings_file(scope)
    with _lock(settings_file):
        settings = read_json(settings_file)
        if settings.get("enabledPlugins", {}).pop(plugin_key, None) is not None:
            write_json(settings_file, settings)
            log(f"Removed from {settings_file}")

    # Remove cached plugin files
    local_path = LOCAL_PLUGINS_DIR / base_name
    if local_path.exists():
        shutil.rmtree(local_path)
        log(f"Removed plugin files from {local_path}")
    elif removed_path and Path(removed_path).exists():
        shutil.rmtree(removed_path)
        log(f"Removed plugin files from {removed_path}")

    success(f"{plugin_name} uninstalled (scope: {scope}). Restart Claude Code to apply.")


def cmd_list() -> None:
    print(f"\n  {Colors.BOLD}ccplugin list{Colors.RESET}\n")

    installed = read_json(INSTALLED_PLUGINS_FILE)
    plugins = installed.get("plugins", {})

    found = False
    for key, records in sorted(plugins.items()):
        for r in records:
            if r.get("installedBy") != "ccplugin":
                continue
            found = True
            parts = [f"scope: {r.get('scope', '?')}"]
            if v := r.get("version"):
                parts.append(f"v{v}")
            if s := r.get("source"):
                # Truncate long Git URLs
                display = s if len(s) <= 60 else s[:57] + "..."
                parts.append(f"from: {display}")
            status = ", ".join(parts)
            log(f"{Colors.CYAN}{key}{Colors.RESET}  ({status})")

    if not found:
        log("No ccplugin-managed plugins installed.")
    print()


def cmd_update(plugin_name: str) -> None:
    print(f"\n  {Colors.BOLD}ccplugin update{Colors.RESET}\n")

    plugin_key = plugin_name if "@" in plugin_name else f"{plugin_name}@local"
    installed = read_json(INSTALLED_PLUGINS_FILE)
    records = installed.get("plugins", {}).get(plugin_key, [])

    if not records:
        error(f'Plugin "{plugin_name}" is not installed via ccplugin.')

    record = next((r for r in records if r.get("installedBy") == "ccplugin" and r.get("source")), None)
    if not record:
        error(f'No ccplugin-managed installation found for "{plugin_name}".')

    source = record["source"]
    if not is_git_url(source):
        warn(f"Source is a local path ({source}). Re-copying...")

    cmd_install(source, record.get("targetName"), record.get("scope", "user"))


def cmd_info(plugin_name: str) -> None:
    print(f"\n  {Colors.BOLD}ccplugin info{Colors.RESET}\n")

    plugin_key = plugin_name if "@" in plugin_name else f"{plugin_name}@local"
    installed = read_json(INSTALLED_PLUGINS_FILE)
    records = installed.get("plugins", {}).get(plugin_key, [])

    if not records:
        error(f'Plugin "{plugin_name}" not found.')

    for r in records:
        install_path = Path(r.get("installPath", ""))

        log(f"{Colors.BOLD}Plugin:{Colors.RESET}     {plugin_key}")
        log(f"{Colors.BOLD}Version:{Colors.RESET}    {r.get('version', 'unknown')}")
        log(f"{Colors.BOLD}Scope:{Colors.RESET}      {r.get('scope', 'unknown')}")
        log(f"{Colors.BOLD}Path:{Colors.RESET}       {r.get('installPath', 'unknown')}")
        log(f"{Colors.BOLD}Source:{Colors.RESET}     {r.get('source', 'unknown')}")
        log(f"{Colors.BOLD}Installed:{Colors.RESET}  {r.get('installedAt', 'unknown')}")
        log(f"{Colors.BOLD}Commit:{Colors.RESET}     {r.get('gitCommitSha', 'n/a')}")
        print()

        # Plugin metadata
        manifest = install_path / ".claude-plugin" / "plugin.json"
        if manifest.exists():
            meta = read_json(manifest)
            if desc := meta.get("description"):
                log(f"{Colors.BOLD}Description:{Colors.RESET} {desc}")
            author = meta.get("author")
            if author:
                author_str = author if isinstance(author, str) else author.get("name", "")
                if author_str:
                    log(f"{Colors.BOLD}Author:{Colors.RESET}      {author_str}")

        # Components
        components: list[str] = []

        skills_dir = install_path / "skills"
        if skills_dir.is_dir():
            skills = [d.name for d in sorted(skills_dir.iterdir()) if d.is_dir()]
            if skills:
                components.append(f"Skills: {', '.join(skills)}")

        commands_dir = install_path / "commands"
        if commands_dir.is_dir():
            cmds = [f.stem for f in sorted(commands_dir.iterdir()) if f.suffix == ".md"]
            if cmds:
                components.append(f"Commands: {', '.join(cmds)}")

        agents_dir = install_path / "agents"
        if agents_dir.is_dir():
            agents = [f.stem for f in sorted(agents_dir.iterdir()) if f.suffix == ".md"]
            if agents:
                components.append(f"Agents: {', '.join(agents)}")

        hooks_file = install_path / "hooks" / "hooks.json"
        if hooks_file.exists():
            components.append("Hooks: configured")

        mcp_file = install_path / ".mcp.json"
        if mcp_file.exists():
            components.append("MCP servers: configured")

        lsp_file = install_path / ".lsp.json"
        if lsp_file.exists():
            components.append("LSP servers: configured")

        if components:
            log(f"{Colors.BOLD}Components:{Colors.RESET}")
            for c in components:
                log(f"  {c}")

    print()

# ── Doctor ──────────────────────────────────────────────────────────────────

def _check(passed: bool, label: str, detail: str = "") -> bool:
    """Print a check result line. Returns pass/fail."""
    icon = f"{Colors.GREEN}✓{Colors.RESET}" if passed else f"{Colors.RED}✗{Colors.RESET}"
    suffix = f"  {Colors.DIM}{detail}{Colors.RESET}" if detail else ""
    log(f"{icon} {label}{suffix}")
    return passed


def _warn_check(label: str, detail: str = "") -> None:
    """Print a warning-level check line."""
    log(f"{Colors.YELLOW}⚠{Colors.RESET} {label}  {Colors.DIM}{detail}{Colors.RESET}" if detail else f"{Colors.YELLOW}⚠{Colors.RESET} {label}")


def _info_check(label: str, detail: str = "") -> None:
    """Print an info-level check line."""
    log(f"{Colors.CYAN}ℹ{Colors.RESET} {label}  {Colors.DIM}{detail}{Colors.RESET}" if detail else f"{Colors.CYAN}ℹ{Colors.RESET} {label}")


def _section(title: str) -> None:
    print(f"\n  {Colors.BOLD}{title}{Colors.RESET}")
    print(f"  {'─' * len(title)}")


def cmd_doctor(verbose: bool = False) -> None:
    print(f"\n  {Colors.BOLD}ccplugin doctor{Colors.RESET}")
    print(f"  Checking environment and managed settings…\n")

    issues: list[str] = []
    warnings: list[str] = []

    # ── 1. Environment ──────────────────────────────────────────────────
    _section("Environment")

    # Git
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
        _check(True, "Git available", result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        _check(False, "Git not found", "Required for remote plugin installs")
        issues.append("Git is not installed — remote installs will fail. Local path installs still work.")

    # Claude Code
    claude_version = None
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True, check=True)
        claude_version = result.stdout.strip()
        _check(True, "Claude Code available", claude_version)
    except (subprocess.CalledProcessError, FileNotFoundError):
        _check(False, "Claude Code not found in PATH")
        issues.append("Claude Code CLI not found — plugins won't load without it.")

    # .claude directory
    _check(CLAUDE_DIR.exists(), "~/.claude directory exists", str(CLAUDE_DIR))

    # Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    _check(sys.version_info >= (3, 9), f"Python {py_ver}", ">=3.9 required")

    # Platform
    _info_check(f"Platform: {_sys}", _platform.platform())

    # ── 2. Managed Settings ─────────────────────────────────────────────
    _section("Managed Settings (IT/Admin)")

    managed = {}
    if MANAGED_SETTINGS_FILE.exists():
        _check(True, f"Managed settings found", str(MANAGED_SETTINGS_FILE))
        managed = read_json(MANAGED_SETTINGS_FILE)

        if verbose:
            log(f"  {Colors.DIM}Keys: {', '.join(managed.keys()) or '(empty)'}{Colors.RESET}")
    else:
        _info_check("No managed settings file found", str(MANAGED_SETTINGS_FILE))
        _info_check("No IT restrictions detected — all features should work")

    # strictKnownMarketplaces
    strict_mkts = managed.get("strictKnownMarketplaces")
    if strict_mkts is not None:
        if isinstance(strict_mkts, list):
            if len(strict_mkts) == 0:
                _check(False, "strictKnownMarketplaces is EMPTY ARRAY",
                       "All marketplace additions are blocked")
                issues.append(
                    "strictKnownMarketplaces is [] — no marketplaces can be added. "
                    "However, ccplugin bypasses this by writing directly to enabledPlugins "
                    "with isLocal: true. Skills, commands, and agents should still load."
                )
            else:
                allowed = []
                for m in strict_mkts:
                    src = m.get("source", "?")
                    if src == "github":
                        allowed.append(m.get("repo", "?"))
                    elif src == "git":
                        allowed.append(m.get("url", "?"))
                    else:
                        allowed.append(f"{src}: {json.dumps(m)}")
                _warn_check(
                    f"strictKnownMarketplaces: {len(strict_mkts)} allowed",
                    ", ".join(allowed),
                )
                warnings.append(
                    f"Marketplace installs restricted to: {', '.join(allowed)}. "
                    "ccplugin bypasses this via local plugin loading."
                )
        else:
            _warn_check("strictKnownMarketplaces has unexpected format", str(type(strict_mkts)))
    else:
        _check(True, "No strictKnownMarketplaces restriction")

    # allowManagedHooksOnly
    hooks_only = managed.get("allowManagedHooksOnly", False)
    if hooks_only:
        _check(False, "allowManagedHooksOnly: true",
               "Plugin hooks will NOT execute")
        issues.append(
            "allowManagedHooksOnly is true — hooks bundled in plugins installed via ccplugin "
            "will be silently ignored. Skills, commands, and agents will still work."
        )
    else:
        _check(True, "Plugin hooks allowed", "allowManagedHooksOnly is not set or false")

    # allowManagedPermissionRulesOnly
    perms_only = managed.get("allowManagedPermissionRulesOnly", False)
    if perms_only:
        _warn_check("allowManagedPermissionRulesOnly: true",
                     "User/project permission overrides blocked")
        warnings.append(
            "allowManagedPermissionRulesOnly is true — plugins can't define custom permission rules. "
            "This generally doesn't affect skills/commands but may limit some plugin features."
        )
    else:
        _check(True, "User permission rules allowed")

    # disableAllHooks
    disable_hooks = managed.get("disableAllHooks", False)
    if disable_hooks:
        _check(False, "disableAllHooks: true", "ALL hooks disabled including plugin hooks")
        issues.append("disableAllHooks is true — no hooks will execute from any source.")
    elif not hooks_only:
        pass  # Already reported above

    # Network sandbox
    sandbox = managed.get("sandbox", {})
    if sandbox:
        sandbox_enabled = sandbox.get("enabled", False)
        network = sandbox.get("network", {})
        allowed_domains = network.get("allowedDomains", [])

        if sandbox_enabled and allowed_domains:
            github_ok = any("github.com" in d for d in allowed_domains)
            _warn_check(
                f"Network sandbox active — {len(allowed_domains)} allowed domains",
                ", ".join(allowed_domains[:5]) + ("..." if len(allowed_domains) > 5 else ""),
            )
            if not github_ok:
                _check(False, "github.com not in allowed domains",
                       "Remote installs from GitHub will fail")
                issues.append(
                    "Network sandbox blocks github.com — remote git clone will fail. "
                    "Use local path installs instead: ccplugin install ./path/to/plugin"
                )
            else:
                _check(True, "github.com is allowed")

            warnings.append(
                "Network sandbox is active. Remote installs may fail for domains "
                "not in the allowlist. Affected installs will fall back to local paths."
            )
        elif sandbox_enabled:
            _warn_check("Sandbox enabled but no domain restrictions")
    else:
        _check(True, "No network sandbox restrictions")

    # permissions.deny — check for git/curl/wget blocks
    perms = managed.get("permissions", {})
    deny_rules = perms.get("deny", [])
    if deny_rules:
        git_blocked = any("git" in r.lower() for r in deny_rules if isinstance(r, str))
        curl_blocked = any("curl" in r.lower() for r in deny_rules if isinstance(r, str))
        wget_blocked = any("wget" in r.lower() for r in deny_rules if isinstance(r, str))

        if git_blocked:
            _warn_check("permissions.deny includes git patterns",
                        "Claude Code can't run git, but ccplugin runs outside Claude Code")
        if curl_blocked or wget_blocked:
            _info_check("permissions.deny blocks curl/wget",
                        "Does not affect ccplugin (runs outside Claude Code)")

        if verbose:
            log(f"  {Colors.DIM}Deny rules: {', '.join(deny_rules[:10])}{Colors.RESET}")

    # disableBypassPermissionsMode
    bypass_disabled = managed.get("permissions", {}).get("disableBypassPermissionsMode")
    if bypass_disabled:
        _info_check("--dangerously-skip-permissions disabled",
                     "Does not affect ccplugin")

    # MCP restrictions
    allowed_mcp = managed.get("allowedMcpServers")
    denied_mcp = managed.get("deniedMcpServers")
    if allowed_mcp is not None or denied_mcp is not None:
        allowed_count = len(allowed_mcp) if isinstance(allowed_mcp, list) else "?"
        denied_count = len(denied_mcp) if isinstance(denied_mcp, list) else "?"
        _warn_check(
            f"MCP server restrictions active",
            f"allowed: {allowed_count}, denied: {denied_count}",
        )
        warnings.append(
            "MCP server restrictions are active. If a plugin bundles MCP servers, "
            "they may be blocked by allowedMcpServers/deniedMcpServers. "
            "Skills, commands, and agents are not affected."
        )
    else:
        _check(True, "No MCP server restrictions")

    # Managed MCP file
    if MANAGED_MCP_FILE.exists():
        _info_check("Managed MCP config found", str(MANAGED_MCP_FILE))

    # forceLoginMethod
    login_method = managed.get("forceLoginMethod")
    if login_method:
        _info_check(f"forceLoginMethod: {login_method}", "Does not affect ccplugin")

    # ── 3. User Settings ────────────────────────────────────────────────
    _section("User Settings")

    user_settings = read_json(USER_SETTINGS_FILE)

    # enabledPlugins key existence (needed for local scope merge)
    has_enabled = "enabledPlugins" in user_settings
    if has_enabled:
        plugin_count = len(user_settings["enabledPlugins"])
        _check(True, f"enabledPlugins in settings.json", f"{plugin_count} plugin(s)")
    else:
        _warn_check("No enabledPlugins key in ~/.claude/settings.json",
                     "Will be created on first install. Required for --scope local to work.")

    # Check for ccplugin-managed plugins
    installed = read_json(INSTALLED_PLUGINS_FILE)
    all_plugins = installed.get("plugins", {})
    cc_managed = {k: v for k, v in all_plugins.items()
                  if any(r.get("installedBy") == "ccplugin" for r in (v if isinstance(v, list) else []))}

    if cc_managed:
        _check(True, f"ccplugin-managed plugins: {len(cc_managed)}")
    else:
        _info_check("No ccplugin-managed plugins installed yet")

    # ── 4. Per-Plugin Health (if any installed) ─────────────────────────
    if cc_managed:
        _section("Plugin Health")

        for key, records in sorted(cc_managed.items()):
            for r in records:
                if r.get("installedBy") != "ccplugin":
                    continue

                install_path = Path(r.get("installPath", ""))
                plugin_ok = True
                name = key

                # Check plugin files exist
                if not install_path.exists():
                    _check(False, f"{name}: install path missing", str(install_path))
                    issues.append(f"{name}: Install path {install_path} does not exist. Run: ccplugin update {name.split('@')[0]}")
                    continue

                manifest = install_path / ".claude-plugin" / "plugin.json"
                if not manifest.exists():
                    _check(False, f"{name}: plugin.json missing")
                    plugin_ok = False

                # Check if enabled in settings
                scope = r.get("scope", "user")
                settings_file = get_settings_file(scope)
                settings = read_json(settings_file)
                enabled = settings.get("enabledPlugins", {}).get(key, False)
                if not enabled:
                    _check(False, f"{name}: not enabled in {settings_file.name}")
                    issues.append(f"{name}: Registered but not enabled. Run: ccplugin install to re-enable.")
                    plugin_ok = False

                # Check components vs. restrictions
                has_hooks = (install_path / "hooks" / "hooks.json").exists()
                has_mcp = (install_path / ".mcp.json").exists()

                component_warnings = []
                if has_hooks and hooks_only:
                    component_warnings.append("hooks (blocked by allowManagedHooksOnly)")
                if has_hooks and disable_hooks:
                    component_warnings.append("hooks (blocked by disableAllHooks)")

                if has_mcp and (allowed_mcp is not None or denied_mcp is not None):
                    # Try to read MCP config and check against restrictions
                    mcp_config = read_json(install_path / ".mcp.json")
                    mcp_servers = list(mcp_config.get("mcpServers", {}).keys())
                    if denied_mcp and isinstance(denied_mcp, list):
                        denied_names = {s.get("serverName", "") for s in denied_mcp if isinstance(s, dict)}
                        blocked = [s for s in mcp_servers if s in denied_names]
                        if blocked:
                            component_warnings.append(f"MCP servers blocked: {', '.join(blocked)}")
                    if allowed_mcp and isinstance(allowed_mcp, list):
                        allowed_names = {s.get("serverName", "") for s in allowed_mcp if isinstance(s, dict)}
                        not_allowed = [s for s in mcp_servers if s not in allowed_names]
                        if not_allowed:
                            component_warnings.append(f"MCP servers not in allowlist: {', '.join(not_allowed)}")

                if component_warnings:
                    _warn_check(f"{name}: partial functionality", "; ".join(component_warnings))
                    warnings.append(f"{name}: Some components restricted — {'; '.join(component_warnings)}")
                elif plugin_ok:
                    _check(True, f"{name}: all components OK (scope: {scope})")

    # ── 5. Summary ──────────────────────────────────────────────────────
    _section("Summary")

    if not issues and not warnings:
        success("All clear! ccplugin should work without restrictions.")
        return

    if issues:
        print(f"\n  {Colors.RED}{Colors.BOLD}Issues ({len(issues)}):{Colors.RESET}")
        for i, issue in enumerate(issues, 1):
            print(f"  {Colors.RED}{i}.{Colors.RESET} {issue}")

    if warnings:
        print(f"\n  {Colors.YELLOW}{Colors.BOLD}Warnings ({len(warnings)}):{Colors.RESET}")
        for i, w in enumerate(warnings, 1):
            print(f"  {Colors.YELLOW}{i}.{Colors.RESET} {w}")

    # Overall verdict
    print()
    if issues:
        blocked_features = []
        working_features = ["skills", "commands", "agents"]

        if any("hook" in i.lower() for i in issues):
            blocked_features.append("hooks")
        if any("mcp" in i.lower() for i in issues):
            blocked_features.append("MCP servers")
        if any("git" in i.lower() or "github" in i.lower() for i in issues):
            blocked_features.append("remote installs")

        log(f"{Colors.BOLD}Verdict:{Colors.RESET} ccplugin can install plugins, but with restrictions.")
        log(f"  {Colors.GREEN}Working:{Colors.RESET} {', '.join(working_features)}")
        if blocked_features:
            log(f"  {Colors.RED}Blocked:{Colors.RESET} {', '.join(blocked_features)}")
    else:
        log(f"{Colors.BOLD}Verdict:{Colors.RESET} ccplugin should work fully. Warnings are informational.")

    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccplugin",
        description="Local Claude Code Plugin Manager — install plugins from any source, bypassing marketplace allowlist restrictions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  ccplugin install https://github.com/obra/superpowers
  ccplugin install https://github.com/anthropics/claude-code feature-dev --scope project
  ccplugin install ./my-local-plugin
  ccplugin uninstall superpowers
  ccplugin update superpowers
  ccplugin list
  ccplugin info superpowers

scopes:
  user      ~/.claude/settings.json (default, available everywhere)
  project   .claude/settings.json (shared via git with team)
  local     .claude/settings.local.json (gitignored, this machine only)
""",
    )

    sub = parser.add_subparsers(dest="command", metavar="command")

    # install
    p_install = sub.add_parser("install", aliases=["i"], help="Install a plugin from Git URL or local path")
    p_install.add_argument("source", help="Git URL or local path to plugin/marketplace")
    p_install.add_argument("plugin_name", nargs="?", default=None, help="Plugin name (if source has multiple)")
    p_install.add_argument("--scope", choices=["user", "project", "local"], default="user")

    # uninstall
    p_uninstall = sub.add_parser("uninstall", aliases=["rm", "remove"], help="Uninstall a plugin")
    p_uninstall.add_argument("plugin_name", help="Plugin name to uninstall")
    p_uninstall.add_argument("--scope", choices=["user", "project", "local"], default="user")

    # update
    p_update = sub.add_parser("update", aliases=["up"], help="Update a plugin from its original source")
    p_update.add_argument("plugin_name", help="Plugin name to update")

    # list
    sub.add_parser("list", aliases=["ls"], help="List ccplugin-managed plugins")

    # info
    p_info = sub.add_parser("info", help="Show plugin details")
    p_info.add_argument("plugin_name", help="Plugin name to inspect")

    # doctor
    p_doctor = sub.add_parser("doctor", help="Check environment, managed settings, and plugin health")
    p_doctor.add_argument("-v", "--verbose", action="store_true", help="Show additional diagnostic details")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    cmd = args.command

    if cmd in ("install", "i"):
        cmd_install(args.source, args.plugin_name, args.scope)
    elif cmd in ("uninstall", "rm", "remove"):
        cmd_uninstall(args.plugin_name, args.scope)
    elif cmd in ("update", "up"):
        cmd_update(args.plugin_name)
    elif cmd in ("list", "ls"):
        cmd_list()
    elif cmd == "info":
        cmd_info(args.plugin_name)
    elif cmd == "doctor":
        cmd_doctor(verbose=args.verbose)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
