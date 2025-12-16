# vikunja-mcp

MCP server for Vikunja task management. Gives Claude full control over projects, tasks, kanban boards, and batch operations.

## Installation

```bash
pip install .
```

## Configuration

Set environment variables:

```bash
export VIKUNJA_URL="https://app.vikunja.cloud"  # Your Vikunja instance
export VIKUNJA_TOKEN="your-api-token"            # From Vikunja settings
export MCP_API_KEY="optional-api-key"            # Enables Bearer token auth
```

## Usage

### Local (stdio)

```bash
vikunja-mcp
```

### Remote (SSE)

```bash
vikunja-mcp --transport sse --port 8000
```

Connect Claude.ai to `http://localhost:8000/sse`

### Authentication

If `MCP_API_KEY` is set, requests require:
- Header: `Authorization: Bearer <key>`
- Or query: `?api_key=<key>`

`/health` endpoint is always public.

## Tools (44)

**Projects:** list, get, create, update, delete, export_all_projects

**Tasks:** list, get, create, update, complete, delete, set_position, add_label, assign, unassign, set_reminders, move_task_to_project

**Labels:** list, create, delete

**Kanban:** list_views, get_view_tasks, list_tasks_by_bucket, set_view_position, get_kanban_view, list_buckets, create_bucket, delete_bucket, sort_bucket

**Relations:** create_task_relation, list_task_relations

**Batch:** batch_create_tasks, batch_update_tasks, batch_set_positions, setup_project

**Bulk:** complete_tasks_by_label, move_tasks_by_label

**Config:** get_project_config, set_project_config, update_project_config, delete_project_config, list_project_configs, create_from_template

## License

MIT
