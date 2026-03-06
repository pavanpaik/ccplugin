Install a Claude Code plugin from a Git URL or local path using ccplugin.

Usage: /ccplugin-install <source> [plugin-name] [--scope user|project|local]

Run the following command and show the output verbatim:

```bash
ccplugin install $ARGUMENTS
```

Examples the user can pass as arguments:
- `https://github.com/org/plugin` — install from a Git URL (user scope)
- `https://github.com/org/repo my-plugin --scope project` — specific plugin, project scope
- `./local-path/my-plugin` — install from a local directory
