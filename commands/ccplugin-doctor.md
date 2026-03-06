Run a health check on the ccplugin environment, managed settings, and installed plugins.

Usage: /ccplugin-doctor [--verbose]

Run the following command and show the output verbatim:

```bash
ccplugin doctor $ARGUMENTS
```

Checks include:
- Git and Claude Code availability
- Managed settings restrictions (strictKnownMarketplaces, allowManagedHooksOnly, network sandbox, MCP allowlists)
- Per-plugin health (install path, settings registration, blocked components)

Pass `--verbose` for additional diagnostic detail.
