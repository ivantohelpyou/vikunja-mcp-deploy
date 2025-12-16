# vikunja-mcp-deploy

Render deployment repo for vikunja-mcp server.

Source: `spawn-solutions/development/projects/impl-1131-vikunja/vikunja-mcp`

## Environment Variables

- `VIKUNJA_URL` - Vikunja instance URL (e.g., `https://app.vikunja.cloud`)
- `VIKUNJA_TOKEN` - API token from Vikunja
- `VIKUNJA_MCP_CONFIG_DIR` - Config directory (set to `/data` for Render disk)

## Local Testing

```bash
export VIKUNJA_URL="https://app.vikunja.cloud"
export VIKUNJA_TOKEN="your-token"
pip install .
vikunja-mcp --transport sse --port 8765
```

## Syncing from Source

```bash
cp ~/spawn-solutions/development/projects/impl-1131-vikunja/vikunja-mcp/src/vikunja_mcp/server.py src/vikunja_mcp/
cp ~/spawn-solutions/development/projects/impl-1131-vikunja/vikunja-mcp/pyproject.toml .
git add -A && git commit -m "Sync from spawn-solutions" && git push
```
