# ccplugin

Local Claude Code Plugin Manager — install plugins from **any** Git repo or local path, bypassing marketplace allowlist restrictions.

Zero external dependencies. Just Python 3.9+ and Git.

## Why?

If your enterprise managed settings restricts Claude Code to a single official marketplace, `claude plugin install` blocks third-party sources. This CLI sidesteps that by:

1. **Cloning** the plugin source (Git repo or local path) to `~/.claude/plugins/cache/`
1. **Copying** it to `~/.claude/plugins/local/<plugin-name>`
1. **Registering** it in `installed_plugins.json` and `settings.json`

Claude Code loads these as local plugins — no marketplace allowlist check.

## Install

```bash
# Option 1: Run directly (single file, no install needed)
chmod +x ccplugin.py
./ccplugin.py install https://github.com/obra/superpowers

# Option 2: pipx (isolated, recommended)
pipx install .

# Option 3: pip
pip install .

# Option 4: Publish to internal PyPI
pip install build twine
python -m build
twine upload --repository internal dist/*
```

## Usage

```bash
# Install from any Git URL
ccplugin install https://github.com/obra/superpowers

# Install a specific plugin from a multi-plugin repo
ccplugin install https://github.com/anthropics/claude-code feature-dev

# Install from a local path
ccplugin install ./my-plugins/custom-review

# Install with scope
ccplugin install https://github.com/some/plugin --scope project
ccplugin install https://github.com/some/plugin --scope local

# Uninstall
ccplugin uninstall superpowers

# Update (re-fetch from original source)
ccplugin update superpowers

# List all ccplugin-managed plugins
ccplugin list

# Show plugin details and components
ccplugin info superpowers

# Check environment, managed settings, and plugin health
ccplugin doctor
ccplugin doctor --verbose
```

## Scopes

| Scope     | Settings file                 | Shared? | Use case                      |
|-----------|-------------------------------|---------|-------------------------------|
| `user`    | `~/.claude/settings.json`     | No      | Personal tools, everywhere    |
| `project` | `.claude/settings.json`       | Yes     | Team plugins, via git         |
| `local`   | `.claude/settings.local.json` | No      | Gitignored, this machine only |

## How It Works

```
ccplugin install https://github.com/org/plugin
│
├─ git clone --depth 1 → ~/.claude/plugins/cache/repo-<hash>
├─ Find .claude-plugin/plugin.json (root or subdirectory)
├─ Copy plugin → ~/.claude/plugins/local/<name>
├─ Register in ~/.claude/plugins/installed_plugins.json
└─ Enable in settings.json (scope-appropriate)
```

The key insight: **managed settings gates `claude plugin install` marketplace checks, but local plugin loading from the filesystem is unrestricted**.

## Enterprise Distribution

```bash
# Build and publish to internal PyPI
python -m build
twine upload --repository-url https://pypi.internal.yourcompany.com/simple/ dist/*

# Team members install with
pip install --index-url https://pypi.internal.yourcompany.com/simple/ ccplugin

# Or just copy the single file — it has zero dependencies
scp ccplugin.py devbox:~/bin/ccplugin
```

## Comparison: Python vs Node.js

|               | Python                  | Node.js               |
|---------------|-------------------------|-----------------------|
| Dependencies  | Zero (stdlib only)      | Zero (built-ins only) |
| Distribution  | Single file, pip, pipx  | npx, npm              |
| Enterprise fit | Python always available | Requires Node.js      |
| Single file   | Yes (`ccplugin.py`)     | Yes (`ccplugin.mjs`)  |
| Package manager | pip/pipx/internal PyPI | npm/internal registry |

## Caveats

- Plugins installed this way won't auto-update via Claude Code's built-in mechanism. Use `ccplugin update` instead.
- You're responsible for vetting plugin source code.
- The `installed_plugins.json` format is reverse-engineered and may change between Claude Code versions.
- Handles the known `settings.local.json` merge bug automatically (creates `enabledPlugins` key in main settings when using `--scope local`).

## Requirements

- Python 3.9+
- Git (for remote sources)
- Claude Code v2.0.12+
