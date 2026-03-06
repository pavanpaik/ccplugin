# ccplugin

Local Claude Code Plugin Manager — install plugins from **any** Git repo or local path using Claude Code's local plugin loading mechanism.

Zero external dependencies. Just Python 3.9+ and Git.

> **Disclaimer:** This tool is an exploration of Claude Code's local plugin loading capabilities. It uses undocumented internal formats that may change between Claude Code versions. Use it to experiment and understand how Claude Code loads plugins locally — not as a way to work around your organisation's policies. Always check with your IT or security team before installing third-party plugins in a managed environment.

## Why?

Claude Code supports loading plugins directly from the local filesystem, independently of the marketplace. This tool makes that mechanism easy to use:

1. **Cloning** the plugin source (Git repo or local path) to `~/.claude/plugins/cache/`
1. **Copying** it to `~/.claude/plugins/local/<plugin-name>`
1. **Registering** it in `installed_plugins.json` and `settings.json`

Claude Code picks these up as local plugins on next launch.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/pavanpaik/ccplugin/main/install.sh | sh
```

Downloads `ccplugin` to `~/.local/bin` and makes it executable. If that directory isn't in your `PATH` yet:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
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

Claude Code's local plugin loader reads directly from the filesystem. This tool writes the records that loader expects.

## Caveats

- Plugins installed this way won't auto-update via Claude Code's built-in mechanism. Use `ccplugin update` instead.
- You're responsible for vetting plugin source code.
- The `installed_plugins.json` format is reverse-engineered and may change between Claude Code versions.
- Handles the known `settings.local.json` merge bug automatically (creates `enabledPlugins` key in main settings when using `--scope local`).

## Native Claude Code Commands

ccplugin ships as a Claude Code plugin itself. Install it as a plugin to get `/ccplugin-*` slash commands directly inside Claude Code:

```bash
# Install ccplugin as a plugin (gives you native slash commands)
ccplugin install https://github.com/pavanpaik/ccplugin
```

Once installed, restart Claude Code and use:

| Slash command | Equivalent CLI |
|---|---|
| `/ccplugin-install <source> [name] [--scope]` | `ccplugin install …` |
| `/ccplugin-uninstall <name> [--scope]` | `ccplugin uninstall …` |
| `/ccplugin-update <name>` | `ccplugin update …` |
| `/ccplugin-list` | `ccplugin list` |
| `/ccplugin-info <name>` | `ccplugin info …` |
| `/ccplugin-doctor [--verbose]` | `ccplugin doctor` |

The slash commands delegate directly to the `ccplugin` CLI binary — they are thin prompt wrappers, not a parallel implementation.

## Requirements

- Python 3.9+
- Git (for remote sources)
- Claude Code v2.0.12+

