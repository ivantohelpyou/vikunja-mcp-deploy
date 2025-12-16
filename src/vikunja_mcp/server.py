"""
Vikunja MCP Server

Exposes Vikunja task management to Claude via Model Context Protocol.

Tools (44):
- Projects: list, get, create, update, delete, export_all_projects
- Tasks: list (w/ label filter), get, create, update, complete, delete, set_position, add_label, assign, unassign, set_reminders, move_task_to_project
- Labels: list, create, delete
- Views: list_views, get_view_tasks, list_tasks_by_bucket, set_view_position, get_kanban_view
- Kanban: list_buckets, create_bucket, delete_bucket, sort_bucket
- Relations: create, list
- Batch: batch_create_tasks, batch_update_tasks, batch_set_positions, setup_project
- Bulk by label: complete_tasks_by_label, move_tasks_by_label
- Config: get_project_config, set_project_config, update_project_config, delete_project_config, list_project_configs, create_from_template

Configuration:
- VIKUNJA_URL: Base URL of Vikunja instance
- VIKUNJA_TOKEN: API authentication token
"""

import bisect
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml
from fastmcp import FastMCP
from pydantic import Field
import requests


# ============================================================================
# PROJECT CONFIG MANAGEMENT
# ============================================================================

# Allow override for Render persistent disk via VIKUNJA_MCP_CONFIG_DIR env var
CONFIG_DIR = Path(os.environ.get("VIKUNJA_MCP_CONFIG_DIR", str(Path.home() / ".vikunja-mcp")))
CONFIG_FILE = CONFIG_DIR / "config.yaml"


def _load_config() -> dict:
    """Load project config from YAML file."""
    if not CONFIG_FILE.exists():
        return {"projects": {}}
    try:
        with open(CONFIG_FILE, "r") as f:
            config = yaml.safe_load(f) or {}
            if "projects" not in config:
                config["projects"] = {}
            return config
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed config file: {e}")


def _save_config(config: dict) -> None:
    """Save project config to YAML file (atomic write)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to temp file, then rename
    fd, temp_path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        os.replace(temp_path, CONFIG_FILE)
    except Exception:
        os.unlink(temp_path)
        raise


def _deep_merge(base: dict, updates: dict) -> dict:
    """Deep merge updates into base dict."""
    result = base.copy()
    for key, value in updates.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# Initialize MCP server
mcp = FastMCP(
    "vikunja",
    instructions="Manage tasks, projects, labels, and kanban boards in Vikunja"
)


# Configuration from environment
def get_config():
    """Get Vikunja configuration from environment."""
    url = os.environ.get("VIKUNJA_URL")
    token = os.environ.get("VIKUNJA_TOKEN")
    if not url or not token:
        raise ValueError("VIKUNJA_URL and VIKUNJA_TOKEN must be set")
    return url.rstrip('/'), token


def _request(method: str, endpoint: str, **kwargs) -> dict:
    """Make authenticated request to Vikunja API."""
    base_url, token = get_config()
    url = f"{base_url}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    response = requests.request(method, url, headers=headers, **kwargs)

    if response.status_code == 401:
        raise ValueError(f"Authentication failed: {response.text}")
    elif response.status_code == 404:
        raise ValueError(f"Resource not found: {response.text}")
    elif response.status_code >= 400:
        raise ValueError(f"API error ({response.status_code}): {response.text}")

    if method != "DELETE":
        return response.json()
    return {}


# Formatters

def _format_task(task: dict) -> dict:
    """Format task for MCP response."""
    reminders = task.get("reminders") or []
    return {
        "id": task["id"],
        "title": task["title"],
        "description": task.get("description", ""),
        "done": task.get("done", False),
        "priority": task.get("priority", 0),
        "position": task.get("position"),  # view-specific position (may be None)
        "start_date": task.get("start_date"),
        "end_date": task.get("end_date"),
        "due_date": task.get("due_date"),
        "reminders": [r.get("reminder") for r in reminders],
        "project_id": task.get("project_id") or task.get("list_id", 0),
        "bucket_id": task.get("bucket_id", 0),
        "labels": [{"id": l["id"], "title": l["title"]} for l in (task.get("labels") or [])],
        "assignees": [{"id": a["id"], "username": a.get("username", "")} for a in (task.get("assignees") or [])],
    }


def _format_project(project: dict) -> dict:
    """Format project for MCP response."""
    return {
        "id": project["id"],
        "title": project["title"],
        "description": project.get("description", ""),
        "parent_project_id": project.get("parent_project_id", 0),
        "hex_color": project.get("hex_color", ""),
    }


def _format_label(label: dict) -> dict:
    """Format label for MCP response."""
    return {
        "id": label["id"],
        "title": label["title"],
        "hex_color": label.get("hex_color", ""),
    }


def _format_bucket(bucket: dict) -> dict:
    """Format bucket for MCP response."""
    return {
        "id": bucket["id"],
        "title": bucket["title"],
        "project_id": bucket.get("project_id") or bucket.get("list_id", 0),
        "position": bucket.get("position", 0),
        "limit": bucket.get("limit", 0),
    }


def _format_view(view: dict) -> dict:
    """Format view for MCP response."""
    return {
        "id": view["id"],
        "title": view["title"],
        "project_id": view.get("project_id", 0),
        "view_kind": view.get("view_kind", ""),
    }


def _format_relation(task_id: int, relation_kind: str, other_task: dict) -> dict:
    """Format task relation for MCP response."""
    return {
        "task_id": task_id,
        "other_task_id": other_task["id"],
        "other_task_title": other_task.get("title", ""),
        "relation_kind": relation_kind,
    }


# ============================================================================
# PROJECT OPERATIONS
# ============================================================================

def _list_projects_impl() -> list[dict]:
    response = _request("GET", "/api/v1/projects")
    return [_format_project(p) for p in response]


def _get_project_impl(project_id: int) -> dict:
    response = _request("GET", f"/api/v1/projects/{project_id}")
    return _format_project(response)


def _create_project_impl(title: str, description: str = "", hex_color: str = "", parent_project_id: int = 0) -> dict:
    data = {"title": title}
    if description:
        data["description"] = description
    if hex_color:
        data["hex_color"] = hex_color
    if parent_project_id:
        data["parent_project_id"] = parent_project_id
    response = _request("PUT", "/api/v1/projects", json=data)
    return _format_project(response)


def _delete_project_impl(project_id: int) -> dict:
    _request("DELETE", f"/api/v1/projects/{project_id}")
    return {"deleted": True, "project_id": project_id}


def _update_project_impl(project_id: int, title: str = "", description: str = "", hex_color: str = "", parent_project_id: int = -1) -> dict:
    """Update a project's properties. Use parent_project_id=0 to move to root."""
    # GET current project state (Vikunja API replaces, so we merge)
    current = _request("GET", f"/api/v1/projects/{project_id}")

    # Only update fields that were explicitly provided
    if title:
        current["title"] = title
    if description:
        current["description"] = description
    if hex_color:
        current["hex_color"] = hex_color
    if parent_project_id >= 0:  # -1 means don't change, 0 means root, >0 means reparent
        current["parent_project_id"] = parent_project_id

    response = _request("POST", f"/api/v1/projects/{project_id}", json=current)
    return _format_project(response)


def _export_all_projects_impl() -> dict:
    """Export all projects and their tasks for backup."""
    projects = _request("GET", "/api/v1/projects")
    export = {
        "exported_at": datetime.now().isoformat(),
        "project_count": len(projects),
        "projects": []
    }

    task_count = 0
    for project in projects:
        project_data = _format_project(project)
        # Get all tasks including completed
        try:
            tasks = _request("GET", f"/api/v1/projects/{project['id']}/tasks")
            project_data["tasks"] = [_format_task(t) for t in tasks]
            task_count += len(project_data["tasks"])
        except Exception:
            project_data["tasks"] = []
            project_data["task_error"] = "Failed to fetch tasks"
        export["projects"].append(project_data)

    export["task_count"] = task_count
    return export


@mcp.tool()
def list_projects() -> list[dict]:
    """
    List all Vikunja projects.

    Returns projects with IDs, titles, descriptions, and parent relationships.
    Use project IDs when creating tasks or listing tasks.
    """
    return _list_projects_impl()


@mcp.tool()
def get_project(
    project_id: int = Field(description="ID of the project to retrieve")
) -> dict:
    """
    Get details of a specific project.

    Returns project ID, title, description, color, and parent project ID.
    """
    return _get_project_impl(project_id)


@mcp.tool()
def create_project(
    title: str = Field(description="Title of the new project"),
    description: str = Field(default="", description="Optional project description"),
    hex_color: str = Field(default="", description="Color in hex format (e.g., '#3498db')"),
    parent_project_id: int = Field(default=0, description="Parent project ID for nesting (0 = top-level)")
) -> dict:
    """
    Create a new Vikunja project.

    Returns the created project with its assigned ID.
    Use parent_project_id to create nested/child projects.
    """
    return _create_project_impl(title, description, hex_color, parent_project_id)


@mcp.tool()
def delete_project(
    project_id: int = Field(description="ID of the project to delete")
) -> dict:
    """
    Delete a project and all its tasks.

    WARNING: This permanently deletes the project and all contained tasks.
    Returns confirmation of deletion.
    """
    return _delete_project_impl(project_id)


@mcp.tool()
def update_project(
    project_id: int = Field(description="ID of the project to update"),
    title: str = Field(default="", description="New title (empty = keep current)"),
    description: str = Field(default="", description="New description (empty = keep current)"),
    hex_color: str = Field(default="", description="New color in hex format (empty = keep current)"),
    parent_project_id: int = Field(default=-1, description="New parent project ID (-1 = keep current, 0 = move to root, >0 = reparent under that project)")
) -> dict:
    """
    Update a project's properties including its parent (reparenting).

    Use parent_project_id to move projects in the hierarchy:
    - -1: Don't change parent (default)
    - 0: Move to root level (top-level project)
    - >0: Move under the specified parent project

    WARNING: Reparenting has known bugs in Vikunja. Back up first with export_all_projects.
    """
    return _update_project_impl(project_id, title, description, hex_color, parent_project_id)


@mcp.tool()
def export_all_projects() -> dict:
    """
    Export all projects and tasks for backup.

    Returns a complete snapshot of all projects with their tasks.
    Use before major restructuring operations.

    Returns: {exported_at, project_count, task_count, projects: [{id, title, ..., tasks: [...]}]}
    """
    return _export_all_projects_impl()


# ============================================================================
# TASK OPERATIONS
# ============================================================================

def _list_tasks_impl(project_id: int, include_completed: bool = False, label_filter: str = "") -> list[dict]:
    response = _request("GET", f"/api/v1/projects/{project_id}/tasks")
    tasks = [_format_task(t) for t in response]
    if not include_completed:
        tasks = [t for t in tasks if not t["done"]]
    if label_filter:
        # Filter by label name (case-insensitive partial match)
        label_lower = label_filter.lower()
        tasks = [t for t in tasks if any(label_lower in l["title"].lower() for l in t["labels"])]
    return tasks


def _get_task_impl(task_id: int) -> dict:
    response = _request("GET", f"/api/v1/tasks/{task_id}")
    return _format_task(response)


def _create_task_impl(project_id: int, title: str, description: str = "", start_date: str = "", end_date: str = "", due_date: str = "", priority: int = 0) -> dict:
    data = {"title": title}
    if description:
        data["description"] = description
    if start_date:
        data["start_date"] = start_date
    if end_date:
        data["end_date"] = end_date
    if due_date:
        data["due_date"] = due_date
    if priority:
        data["priority"] = priority
    response = _request("PUT", f"/api/v1/projects/{project_id}/tasks", json=data)
    return _format_task(response)


def _update_task_impl(task_id: int, title: str = "", description: str = "", start_date: str = "", end_date: str = "", due_date: str = "", priority: int = -1) -> dict:
    # Vikunja API replaces the task, so we must GET first and merge changes
    current = _request("GET", f"/api/v1/tasks/{task_id}")

    # Only update fields that were explicitly provided
    if title:
        current["title"] = title
    if description:
        current["description"] = description
    if start_date:
        current["start_date"] = start_date
    if end_date:
        current["end_date"] = end_date
    if due_date:
        current["due_date"] = due_date
    if priority >= 0:
        current["priority"] = priority

    response = _request("POST", f"/api/v1/tasks/{task_id}", json=current)
    return _format_task(response)


def _complete_task_impl(task_id: int) -> dict:
    # GET first to preserve other fields
    current = _request("GET", f"/api/v1/tasks/{task_id}")
    current["done"] = True
    response = _request("POST", f"/api/v1/tasks/{task_id}", json=current)
    return _format_task(response)


def _delete_task_impl(task_id: int) -> dict:
    _request("DELETE", f"/api/v1/tasks/{task_id}")
    return {"deleted": True, "task_id": task_id}


def _set_task_position_impl(
    task_id: int,
    project_id: int,
    view_id: int,
    bucket_id: int,
    apply_sort: bool = False
) -> dict:
    """
    Move a task to a kanban bucket.

    If apply_sort=True, calculates the correct position based on the bucket's
    sort strategy from project config (instead of just appending).
    """
    # Add task to bucket
    bucket_data = {
        "task_id": task_id,
        "bucket_id": bucket_id,
        "project_view_id": view_id,
        "project_id": project_id
    }
    _request("POST", f"/api/v1/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks", json=bucket_data)

    result = {"task_id": task_id, "bucket_id": bucket_id, "view_id": view_id, "position_set": False}

    if not apply_sort:
        return result

    # Get project config for sort strategy
    config_result = _get_project_config_impl(project_id)
    project_config = config_result.get("config")
    if not project_config:
        return result

    sort_strategy = project_config.get("sort_strategy", {})
    default_strategy = sort_strategy.get("default", "manual")
    bucket_strategies = sort_strategy.get("buckets", {})

    # Get bucket name from bucket_id
    buckets = _list_buckets_impl(project_id, view_id)
    bucket_name = None
    for b in buckets:
        if b["id"] == bucket_id:
            bucket_name = b["title"]
            break

    if not bucket_name:
        return result

    # Get sort strategy for this bucket
    strategy = bucket_strategies.get(bucket_name, default_strategy)
    if strategy == "manual":
        return result

    # Fetch the task to get its sort key value
    task = _get_task_impl(task_id)

    # Fetch existing tasks in bucket with positions
    existing_raw = _get_bucket_tasks_raw(project_id, view_id, bucket_id)
    # Filter out the task we just moved (it's now in the bucket)
    existing_raw = [t for t in existing_raw if t["id"] != task_id]

    # Build sorted list of (sort_key, position) for existing tasks
    existing_sorted = []
    for t in existing_raw:
        key = _get_task_sort_key(t, strategy)
        pos = t.get("position", 0)
        existing_sorted.append((key, pos))
    existing_sorted.sort(key=lambda x: x[0])

    # Get sort key for the moved task
    new_key = _get_task_sort_key(task, strategy)

    # Extract just the sort keys for bisect
    existing_keys = [x[0] for x in existing_sorted]

    # Binary search to find insertion point
    insert_idx = bisect.bisect_left(existing_keys, new_key)

    # Calculate position between neighbors
    if not existing_sorted:
        new_pos = 1000.0
    elif insert_idx == 0:
        first_pos = existing_sorted[0][1]
        new_pos = first_pos / 2 if first_pos > 0 else -1000.0
    elif insert_idx >= len(existing_sorted):
        last_pos = existing_sorted[-1][1]
        new_pos = last_pos + 1000.0
    else:
        prev_pos = existing_sorted[insert_idx - 1][1]
        next_pos = existing_sorted[insert_idx][1]
        new_pos = (prev_pos + next_pos) / 2

    # Set the position
    _set_view_position_impl(task_id, view_id, new_pos)
    result["position_set"] = True
    result["position"] = new_pos

    return result


def _add_label_to_task_impl(task_id: int, label_id: int) -> dict:
    _request("PUT", f"/api/v1/tasks/{task_id}/labels", json={"label_id": label_id})
    return {"task_id": task_id, "label_id": label_id, "added": True}


def _assign_user_impl(task_id: int, user_id: int) -> dict:
    _request("PUT", f"/api/v1/tasks/{task_id}/assignees", json={"user_id": user_id})
    return {"task_id": task_id, "user_id": user_id, "assigned": True}


def _unassign_user_impl(task_id: int, user_id: int) -> dict:
    _request("DELETE", f"/api/v1/tasks/{task_id}/assignees/{user_id}")
    return {"task_id": task_id, "user_id": user_id, "unassigned": True}


@mcp.tool()
def list_tasks(
    project_id: int = Field(description="ID of the project to list tasks from"),
    include_completed: bool = Field(default=False, description="Whether to include completed tasks"),
    label_filter: str = Field(default="", description="Filter by label name (case-insensitive partial match, e.g., 'Sourdough' or '🍞')")
) -> list[dict]:
    """
    List tasks in a Vikunja project.

    Returns tasks with IDs, titles, descriptions, priorities, due dates, labels, and assignees.
    By default excludes completed tasks. Use include_completed=true to see all.
    Use label_filter to find tasks with specific labels (e.g., label_filter="Sourdough").
    """
    return _list_tasks_impl(project_id, include_completed, label_filter)


@mcp.tool()
def get_task(
    task_id: int = Field(description="ID of the task to retrieve")
) -> dict:
    """
    Get details of a specific task.

    Returns full task details including labels, assignees, and bucket placement.
    """
    return _get_task_impl(task_id)


@mcp.tool()
def create_task(
    project_id: int = Field(description="ID of the project to create the task in"),
    title: str = Field(description="Title of the task"),
    description: str = Field(default="", description="Optional task description"),
    start_date: str = Field(default="", description="Start date in ISO format - for GANTT chart"),
    end_date: str = Field(default="", description="End date in ISO format - for GANTT chart"),
    due_date: str = Field(default="", description="Due date in ISO format - for deadlines/Upcoming view"),
    priority: int = Field(default=0, description="Priority: 0=none, 1=low, 2=medium, 3=high, 4=urgent, 5=critical")
) -> dict:
    """
    Create a new task in a Vikunja project.

    Date fields (ISO format YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ):
    - start_date + end_date: Required for GANTT chart display
    - due_date: Used for deadlines and "Upcoming" view

    GANTT VISIBILITY: Vikunja Gantt shows DAILY resolution only. Tasks spanning
    hours within a single day appear as tiny bars. For visible Gantt bars, use
    full-day spans: start_date="YYYY-MM-DDT00:00:00Z", end_date="YYYY-MM-DDT23:59:00Z".
    Put actual times in task title if needed (e.g., "Bake pie (2pm-4pm)").
    Override this if precise time tracking is more important than Gantt visibility.

    Priority levels: 0=none, 1=low, 2=medium, 3=high, 4=urgent, 5=critical
    """
    return _create_task_impl(project_id, title, description, start_date, end_date, due_date, priority)


@mcp.tool()
def update_task(
    task_id: int = Field(description="ID of the task to update"),
    title: str = Field(default="", description="New title (empty = keep current)"),
    description: str = Field(default="", description="New description (empty = keep current)"),
    start_date: str = Field(default="", description="Start date in ISO format - for GANTT (empty = keep current)"),
    end_date: str = Field(default="", description="End date in ISO format - for GANTT (empty = keep current)"),
    due_date: str = Field(default="", description="Due date in ISO format - for deadlines (empty = keep current)"),
    priority: int = Field(default=-1, description="New priority (-1 = keep current, 0-5 to set)")
) -> dict:
    """
    Update an existing task.

    Only specified fields are updated. Use empty strings or -1 to keep current values.
    For GANTT chart: set start_date + end_date. For deadlines: set due_date.
    """
    return _update_task_impl(task_id, title, description, start_date, end_date, due_date, priority)


@mcp.tool()
def complete_task(
    task_id: int = Field(description="ID of the task to mark as complete")
) -> dict:
    """
    Mark a task as complete (done=true).

    Returns the updated task.
    """
    return _complete_task_impl(task_id)


@mcp.tool()
def delete_task(
    task_id: int = Field(description="ID of the task to delete")
) -> dict:
    """
    Delete a task permanently.

    Returns confirmation of deletion.
    """
    return _delete_task_impl(task_id)


@mcp.tool()
def set_task_position(
    task_id: int = Field(description="ID of the task to move"),
    project_id: int = Field(description="ID of the project containing the task"),
    view_id: int = Field(description="ID of the kanban view (get from get_kanban_view)"),
    bucket_id: int = Field(description="ID of the target bucket (get from list_buckets)"),
    apply_sort: bool = Field(default=False, description="If true, calculate correct position based on bucket's sort strategy from project config")
) -> dict:
    """
    Move a task to a kanban bucket.

    First use get_kanban_view to get the view_id, then list_buckets to find bucket_id.

    If apply_sort=True, the task will be positioned according to the bucket's
    sort_strategy from project config (e.g., by start_date). Otherwise, it's
    appended to the bucket.
    """
    return _set_task_position_impl(task_id, project_id, view_id, bucket_id, apply_sort)


@mcp.tool()
def add_label_to_task(
    task_id: int = Field(description="ID of the task"),
    label_id: int = Field(description="ID of the label to add (get from list_labels)")
) -> dict:
    """
    Add a label to a task.

    Use list_labels to find available label IDs.
    """
    return _add_label_to_task_impl(task_id, label_id)


@mcp.tool()
def assign_user(
    task_id: int = Field(description="ID of the task"),
    user_id: int = Field(description="ID of the user to assign")
) -> dict:
    """
    Assign a user to a task.

    Returns confirmation of assignment.
    """
    return _assign_user_impl(task_id, user_id)


@mcp.tool()
def unassign_user(
    task_id: int = Field(description="ID of the task"),
    user_id: int = Field(description="ID of the user to unassign")
) -> dict:
    """
    Remove a user from a task.

    Returns confirmation of removal.
    """
    return _unassign_user_impl(task_id, user_id)


def _format_reminder_input(reminder: str) -> dict:
    """Format a reminder datetime string into API format."""
    return {
        "reminder": reminder,
        "relative_period": 0,
        "relative_to": ""
    }


def _set_reminders_impl(task_id: int, reminders: list[str]) -> dict:
    """Set reminders on a task. Replaces all existing reminders."""
    # GET current task to preserve other fields
    current = _request("GET", f"/api/v1/tasks/{task_id}")
    # Convert datetime strings to reminder objects with required fields
    current["reminders"] = [_format_reminder_input(r) for r in reminders]
    response = _request("POST", f"/api/v1/tasks/{task_id}", json=current)
    return _format_task(response)


@mcp.tool()
def set_reminders(
    task_id: int = Field(description="ID of the task"),
    reminders: list[str] = Field(description="List of reminder datetimes in ISO format (e.g., ['2025-12-20T10:00:00Z']). Pass empty list to clear all reminders.")
) -> dict:
    """
    Set reminders on a task.

    Replaces all existing reminders with the provided list.
    Each reminder is an ISO datetime when a notification will be sent.
    Pass an empty list to clear all reminders.

    Example: reminders=["2025-12-19T09:00:00Z", "2025-12-19T13:00:00Z"]
    """
    return _set_reminders_impl(task_id, reminders)


# ============================================================================
# LABEL OPERATIONS
# ============================================================================

def _list_labels_impl() -> list[dict]:
    response = _request("GET", "/api/v1/labels")
    return [_format_label(l) for l in response]


def _create_label_impl(title: str, hex_color: str) -> dict:
    data = {"title": title, "hex_color": hex_color}
    response = _request("PUT", "/api/v1/labels", json=data)
    return _format_label(response)


def _delete_label_impl(label_id: int) -> dict:
    _request("DELETE", f"/api/v1/labels/{label_id}")
    return {"deleted": True, "label_id": label_id}


@mcp.tool()
def list_labels() -> list[dict]:
    """
    List all available labels.

    Returns labels with IDs, titles, and colors.
    Use label IDs with add_label_to_task.
    """
    return _list_labels_impl()


@mcp.tool()
def create_label(
    title: str = Field(description="Label title"),
    hex_color: str = Field(description="Color in hex format (e.g., '#FF0000' for red)")
) -> dict:
    """
    Create a new label.

    Returns the created label with its assigned ID.
    """
    return _create_label_impl(title, hex_color)


@mcp.tool()
def delete_label(
    label_id: int = Field(description="ID of the label to delete")
) -> dict:
    """
    Delete a label.

    Returns confirmation of deletion.
    """
    return _delete_label_impl(label_id)


# ============================================================================
# VIEW OPERATIONS
# ============================================================================

def _list_views_impl(project_id: int) -> list[dict]:
    """List all views for a project (list, kanban, gantt, table)."""
    response = _request("GET", f"/api/v1/projects/{project_id}/views")
    return [_format_view(v) for v in response]


def _get_view_tasks_impl(project_id: int, view_id: int) -> list[dict]:
    """Get tasks via a specific view endpoint - returns tasks with bucket info for kanban views."""
    response = _request("GET", f"/api/v1/projects/{project_id}/views/{view_id}/tasks")
    # Kanban views return buckets with nested tasks
    # List/Gantt views return flat task arrays
    tasks = []
    for item in response:
        if "tasks" in item:
            # This is a bucket - extract tasks with bucket info
            bucket_id = item["id"]
            bucket_title = item["title"]
            for task in (item.get("tasks") or []):
                formatted = _format_task(task)
                formatted["bucket_id"] = bucket_id
                formatted["bucket_title"] = bucket_title
                tasks.append(formatted)
        else:
            # This is a task (non-kanban view)
            tasks.append(_format_task(item))
    return tasks


def _list_tasks_by_bucket_impl(project_id: int, view_id: int) -> dict:
    """Get tasks grouped by bucket for kanban views."""
    response = _request("GET", f"/api/v1/projects/{project_id}/views/{view_id}/tasks")
    buckets = {}
    for item in response:
        if "tasks" in item:
            bucket_name = item["title"]
            buckets[bucket_name] = {
                "bucket_id": item["id"],
                "tasks": [_format_task(t) for t in (item.get("tasks") or [])]
            }
    return buckets


def _get_bucket_tasks_raw(project_id: int, view_id: int, bucket_id: int) -> list[dict]:
    """Get raw tasks in a specific bucket (includes position field)."""
    response = _request("GET", f"/api/v1/projects/{project_id}/views/{view_id}/tasks")
    for item in response:
        if item.get("id") == bucket_id and "tasks" in item:
            return item.get("tasks") or []
    return []


def _get_task_sort_key(task: dict, strategy: str):
    """Extract sort key from a task dict (API response format)."""
    if strategy == "start_date":
        return task.get("start_date") or "9999-12-31"
    elif strategy == "due_date":
        return task.get("due_date") or "9999-12-31"
    elif strategy == "end_date":
        return task.get("end_date") or "9999-12-31"
    elif strategy == "priority":
        return -(task.get("priority") or 0)
    elif strategy == "alphabetical":
        return (task.get("title") or "").lower()
    elif strategy == "created":
        return task.get("id") or 0
    return 0


def _get_input_sort_key(task_input: dict, created_task: dict, strategy: str):
    """Extract sort key from task input (used during batch create)."""
    if strategy == "start_date":
        return task_input.get("start_date") or "9999-12-31"
    elif strategy == "due_date":
        return task_input.get("due_date") or "9999-12-31"
    elif strategy == "end_date":
        return task_input.get("end_date") or "9999-12-31"
    elif strategy == "priority":
        return -(task_input.get("priority") or 0)
    elif strategy == "alphabetical":
        return task_input.get("title", "").lower()
    elif strategy == "created":
        return created_task.get("id") or 0
    return 0


def _set_view_position_impl(task_id: int, view_id: int, position: float) -> dict:
    """Set a task's position within a specific view (for Gantt ordering, etc.)."""
    response = _request("POST", f"/api/v1/tasks/{task_id}/position", json={
        "project_view_id": view_id,
        "position": position
    })
    return response


def _get_kanban_view_impl(project_id: int) -> dict:
    response = _request("GET", f"/api/v1/projects/{project_id}/views")
    kanban_views = [v for v in response if v.get("view_kind") == "kanban"]
    if not kanban_views:
        raise ValueError(f"No kanban view found for project {project_id}")
    return _format_view(kanban_views[0])


def _list_buckets_impl(project_id: int, view_id: int) -> list[dict]:
    response = _request("GET", f"/api/v1/projects/{project_id}/views/{view_id}/buckets")
    return [_format_bucket(b) for b in response]


def _create_bucket_impl(project_id: int, view_id: int, title: str, position: int = 0, limit: int = 0) -> dict:
    data = {"title": title, "position": position, "limit": limit}
    response = _request("PUT", f"/api/v1/projects/{project_id}/views/{view_id}/buckets", json=data)
    return _format_bucket(response)


def _delete_bucket_impl(project_id: int, view_id: int, bucket_id: int) -> dict:
    _request("DELETE", f"/api/v1/projects/{project_id}/views/{view_id}/buckets/{bucket_id}")
    return {"deleted": True, "bucket_id": bucket_id}


@mcp.tool()
def list_views(
    project_id: int = Field(description="ID of the project")
) -> list[dict]:
    """
    List all views for a project.

    Returns views with IDs, titles, and view_kind (list, kanban, gantt, table).
    Use view IDs with get_view_tasks to fetch tasks via that view.
    """
    return _list_views_impl(project_id)


@mcp.tool()
def get_view_tasks(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the view (get from list_views)")
) -> list[dict]:
    """
    Get tasks via a specific view endpoint.

    For kanban views, returns tasks with bucket_id and bucket_title populated.
    For list/gantt views, returns flat task list.
    Use list_tasks_by_bucket for grouped kanban view.
    """
    return _get_view_tasks_impl(project_id, view_id)


@mcp.tool()
def list_tasks_by_bucket(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the kanban view (get from list_views)")
) -> dict:
    """
    Get tasks grouped by kanban bucket.

    Returns dict with bucket names as keys, each containing bucket_id and tasks array.
    Use this to understand workflow state without asking user which bucket tasks are in.

    Example response: {"📝 To-Do": {"bucket_id": 123, "tasks": [...]}, "🔥 In Progress": {...}}
    """
    return _list_tasks_by_bucket_impl(project_id, view_id)


@mcp.tool()
def set_view_position(
    task_id: int = Field(description="ID of the task"),
    view_id: int = Field(description="ID of the view (Gantt, List, etc.)"),
    position: float = Field(description="Position value (lower = earlier in list)")
) -> dict:
    """
    Set a task's position within a specific view.

    Use this to order tasks in Gantt, List, or Table views.
    Position is a float - use increments (e.g., 1000, 2000, 3000) for easy reordering.
    """
    return _set_view_position_impl(task_id, view_id, position)


@mcp.tool()
def get_kanban_view(
    project_id: int = Field(description="ID of the project")
) -> dict:
    """
    Get the kanban view for a project.

    Returns the view ID needed for bucket operations and task positioning.
    Every project has a default kanban view created automatically.
    """
    return _get_kanban_view_impl(project_id)


@mcp.tool()
def list_buckets(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the view (get from get_kanban_view)")
) -> list[dict]:
    """
    List all kanban buckets (columns) in a view.

    Returns buckets with IDs, titles, positions, and WIP limits.
    Use bucket IDs with set_task_position to move tasks.
    """
    return _list_buckets_impl(project_id, view_id)


@mcp.tool()
def create_bucket(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the view (get from get_kanban_view)"),
    title: str = Field(description="Bucket/column title"),
    position: int = Field(default=0, description="Sort position (0 = first)"),
    limit: int = Field(default=0, description="WIP limit (0 = no limit)")
) -> dict:
    """
    Create a new kanban bucket (column).

    Returns the created bucket with its assigned ID.
    """
    return _create_bucket_impl(project_id, view_id, title, position, limit)


@mcp.tool()
def delete_bucket(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the view (get from get_kanban_view)"),
    bucket_id: int = Field(description="ID of the bucket to delete")
) -> dict:
    """
    Delete a kanban bucket (column).

    WARNING: Tasks in this bucket may be moved to another bucket or become unassigned.
    Returns confirmation of deletion.
    """
    return _delete_bucket_impl(project_id, view_id, bucket_id)


# ============================================================================
# RELATION OPERATIONS
# ============================================================================

def _create_task_relation_impl(task_id: int, relation_kind: str, other_task_id: int) -> dict:
    data = {"other_task_id": other_task_id, "relation_kind": relation_kind}
    response = _request("PUT", f"/api/v1/tasks/{task_id}/relations", json=data)
    return {"task_id": task_id, "other_task_id": other_task_id, "relation_kind": relation_kind, "created": True}


def _list_task_relations_impl(task_id: int) -> list[dict]:
    response = _request("GET", f"/api/v1/tasks/{task_id}")
    relations = []
    related_tasks = response.get("related_tasks") or {}
    for relation_kind, tasks in related_tasks.items():
        if tasks:
            for task in tasks:
                relations.append(_format_relation(task_id, relation_kind, task))
    return relations


@mcp.tool()
def create_task_relation(
    task_id: int = Field(description="ID of the source task"),
    relation_kind: str = Field(description="Relation type: 'subtask', 'parenttask', 'related', 'blocking', 'blocked', 'duplicateof', 'duplicates', 'precedes', 'follows', 'copiedfrom', 'copiedto'"),
    other_task_id: int = Field(description="ID of the target task")
) -> dict:
    """
    Create a relation between two tasks.

    Relation types:
    - subtask/parenttask: Parent-child relationship
    - blocking/blocked: Task dependencies
    - related: General association
    - precedes/follows: Sequential ordering
    - duplicateof/duplicates: Duplicate tracking
    """
    return _create_task_relation_impl(task_id, relation_kind, other_task_id)


@mcp.tool()
def list_task_relations(
    task_id: int = Field(description="ID of the task")
) -> list[dict]:
    """
    List all relations for a task.

    Returns relations showing how this task connects to other tasks
    (blocking, subtasks, related, etc.).
    """
    return _list_task_relations_impl(task_id)


# ============================================================================
# BATCH OPERATIONS
# ============================================================================

def _batch_create_tasks_impl(
    project_id: int,
    tasks: list[dict],
    create_missing_labels: bool = True,
    create_missing_buckets: bool = False,
    use_project_config: bool = True,
    apply_sort: bool = True,
    apply_default_labels: bool = False
) -> dict:
    """
    Create multiple tasks with labels, relations, and bucket positions.

    Task schema:
    {
        "title": str,              # required
        "description": str,        # optional
        "start_date": str,         # optional, ISO format (for GANTT)
        "end_date": str,           # optional, ISO format (for GANTT)
        "due_date": str,           # optional, ISO format (for deadlines)
        "priority": int,           # optional, 0-5
        "labels": list[str],       # optional, label names
        "bucket": str,             # optional, bucket name
        "ref": str,                # optional, local reference for relations
        "blocked_by": list[str],   # optional, refs of blocking tasks
        "blocks": list[str],       # optional, refs this task blocks
        "subtask_of": str,         # optional, ref of parent task
    }

    If use_project_config=True, applies default_bucket from config.
    If apply_default_labels=True (opt-in), applies default_labels from config to tasks without labels.
    If apply_sort=True, auto-positions tasks based on sort_strategy in config.
    """
    # Load project config if enabled
    project_config = None
    if use_project_config:
        config_result = _get_project_config_impl(project_id)
        project_config = config_result.get("config")

    # Apply config defaults to tasks
    if project_config:
        default_labels = project_config.get("default_labels", [])
        default_bucket = project_config.get("default_bucket", "")

        for task in tasks:
            # Apply default labels only if opt-in and task doesn't specify any
            if apply_default_labels and not task.get("labels") and default_labels:
                task["labels"] = default_labels.copy()
            # Apply default bucket if task doesn't specify one
            if not task.get("bucket") and default_bucket:
                task["bucket"] = default_bucket
    result = {
        "created": 0,
        "tasks": [],
        "labels_created": [],
        "relations_created": 0,
        "errors": []
    }

    # Step 1: Fetch existing labels and build name→id map
    existing_labels = _list_labels_impl()
    label_map = {l["title"]: l["id"] for l in existing_labels}

    # Step 2: Find all label names needed
    needed_labels = set()
    for task in tasks:
        for label_name in task.get("labels", []):
            if label_name not in label_map:
                needed_labels.add(label_name)

    # Step 3: Create missing labels if enabled
    if create_missing_labels and needed_labels:
        # Default colors for auto-created labels
        colors = ["#3498db", "#e74c3c", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c"]
        for i, label_name in enumerate(needed_labels):
            try:
                new_label = _create_label_impl(label_name, colors[i % len(colors)])
                label_map[label_name] = new_label["id"]
                result["labels_created"].append(label_name)
            except Exception as e:
                result["errors"].append(f"Failed to create label '{label_name}': {str(e)}")

    # Step 4: Fetch kanban view and buckets for bucket positioning
    view_id = None
    bucket_map = {}  # name → id

    # Check if any task needs bucket positioning
    needs_buckets = any(task.get("bucket") for task in tasks)
    if needs_buckets:
        try:
            view = _get_kanban_view_impl(project_id)
            view_id = view["id"]
            existing_buckets = _list_buckets_impl(project_id, view_id)
            bucket_map = {b["title"]: b["id"] for b in existing_buckets}

            # Create missing buckets if enabled
            if create_missing_buckets:
                needed_buckets = set()
                for task in tasks:
                    bucket_name = task.get("bucket")
                    if bucket_name and bucket_name not in bucket_map:
                        needed_buckets.add(bucket_name)

                for i, bucket_name in enumerate(needed_buckets):
                    try:
                        new_bucket = _create_bucket_impl(project_id, view_id, bucket_name, position=len(existing_buckets) + i)
                        bucket_map[bucket_name] = new_bucket["id"]
                    except Exception as e:
                        result["errors"].append(f"Failed to create bucket '{bucket_name}': {str(e)}")
        except Exception as e:
            result["errors"].append(f"Failed to get kanban view: {str(e)}")

    # Step 5: Create all tasks and build ref→id map
    ref_map = {}  # ref → task_id
    created_tasks = []  # list of (task_input, created_task)

    for task_input in tasks:
        try:
            created_task = _create_task_impl(
                project_id=project_id,
                title=task_input["title"],
                description=task_input.get("description", ""),
                start_date=task_input.get("start_date", ""),
                end_date=task_input.get("end_date", ""),
                due_date=task_input.get("due_date", ""),
                priority=task_input.get("priority", 0)
            )

            result["created"] += 1
            result["tasks"].append({
                "ref": task_input.get("ref"),
                "id": created_task["id"],
                "title": created_task["title"]
            })

            # Track ref for relations
            ref = task_input.get("ref")
            if ref:
                ref_map[ref] = created_task["id"]

            created_tasks.append((task_input, created_task))

        except Exception as e:
            result["errors"].append(f"Failed to create task '{task_input.get('title', '?')}': {str(e)}")

    # Step 6: Add labels to tasks
    for task_input, created_task in created_tasks:
        for label_name in task_input.get("labels", []):
            label_id = label_map.get(label_name)
            if label_id:
                try:
                    _add_label_to_task_impl(created_task["id"], label_id)
                except Exception as e:
                    result["errors"].append(f"Failed to add label '{label_name}' to task {created_task['id']}: {str(e)}")
            else:
                result["errors"].append(f"Label '{label_name}' not found for task {created_task['id']}")

    # Step 7: Create relations
    for task_input, created_task in created_tasks:
        task_id = created_task["id"]

        # blocked_by: this task is blocked by other tasks
        for blocker_ref in task_input.get("blocked_by", []):
            blocker_id = ref_map.get(blocker_ref)
            if blocker_id:
                try:
                    _create_task_relation_impl(task_id, "blocked", blocker_id)
                    result["relations_created"] += 1
                except Exception as e:
                    result["errors"].append(f"Failed to create blocked relation for task {task_id}: {str(e)}")
            else:
                result["errors"].append(f"Unknown ref '{blocker_ref}' in blocked_by for task {task_id}")

        # blocks: this task blocks other tasks
        for blocked_ref in task_input.get("blocks", []):
            blocked_id = ref_map.get(blocked_ref)
            if blocked_id:
                try:
                    _create_task_relation_impl(task_id, "blocking", blocked_id)
                    result["relations_created"] += 1
                except Exception as e:
                    result["errors"].append(f"Failed to create blocking relation for task {task_id}: {str(e)}")
            else:
                result["errors"].append(f"Unknown ref '{blocked_ref}' in blocks for task {task_id}")

        # subtask_of: this task is a subtask of another
        parent_ref = task_input.get("subtask_of")
        if parent_ref:
            parent_id = ref_map.get(parent_ref)
            if parent_id:
                try:
                    _create_task_relation_impl(task_id, "parenttask", parent_id)
                    result["relations_created"] += 1
                except Exception as e:
                    result["errors"].append(f"Failed to create subtask relation for task {task_id}: {str(e)}")
            else:
                result["errors"].append(f"Unknown ref '{parent_ref}' in subtask_of for task {task_id}")

    # Step 8: Set bucket positions
    if view_id and bucket_map:
        for task_input, created_task in created_tasks:
            bucket_name = task_input.get("bucket")
            if bucket_name:
                bucket_id = bucket_map.get(bucket_name)
                if bucket_id:
                    try:
                        _set_task_position_impl(created_task["id"], project_id, view_id, bucket_id)
                    except Exception as e:
                        result["errors"].append(f"Failed to set bucket for task {created_task['id']}: {str(e)}")
                else:
                    result["errors"].append(f"Bucket '{bucket_name}' not found for task {created_task['id']}")

    # Step 9: Auto-sort tasks based on project config sort_strategy
    # This finds the correct insertion point among existing tasks
    if apply_sort and project_config and view_id:
        sort_strategy = project_config.get("sort_strategy", {})
        default_strategy = sort_strategy.get("default", "manual")
        bucket_strategies = sort_strategy.get("buckets", {})

        # Group newly created tasks by bucket
        tasks_by_bucket = {}  # bucket_name → [(task_input, created_task)]
        for task_input, created_task in created_tasks:
            bucket_name = task_input.get("bucket")
            if bucket_name:
                if bucket_name not in tasks_by_bucket:
                    tasks_by_bucket[bucket_name] = []
                tasks_by_bucket[bucket_name].append((task_input, created_task))

        # Sort and position tasks in each bucket
        for bucket_name, bucket_tasks in tasks_by_bucket.items():
            strategy = bucket_strategies.get(bucket_name, default_strategy)

            if strategy == "manual":
                # Manual: skip auto-sort (bucket position already set in Step 8)
                continue

            bucket_id = bucket_map.get(bucket_name)
            if not bucket_id:
                continue

            # Fetch existing tasks in bucket with positions
            try:
                existing_raw = _get_bucket_tasks_raw(project_id, view_id, bucket_id)
            except Exception as e:
                result["errors"].append(f"Failed to fetch existing tasks in bucket '{bucket_name}': {str(e)}")
                continue

            # Filter out the newly created tasks (they're already in the bucket from Step 8)
            new_task_ids = {created_task["id"] for _, created_task in bucket_tasks}
            existing_raw = [t for t in existing_raw if t["id"] not in new_task_ids]

            # Build sorted list of (sort_key, position) for existing tasks
            existing_sorted = []
            for task in existing_raw:
                key = _get_task_sort_key(task, strategy)
                pos = task.get("position", 0)
                existing_sorted.append((key, pos))
            existing_sorted.sort(key=lambda x: x[0])

            # Extract just the sort keys for bisect
            existing_keys = [x[0] for x in existing_sorted]

            # For each new task, find insertion point and calculate position
            for task_input, created_task in bucket_tasks:
                new_key = _get_input_sort_key(task_input, created_task, strategy)

                # Binary search to find insertion point
                insert_idx = bisect.bisect_left(existing_keys, new_key)

                # Calculate position between neighbors
                if not existing_sorted:
                    # No existing tasks - use standard position
                    new_pos = 1000.0
                elif insert_idx == 0:
                    # Insert at beginning - half of first position
                    first_pos = existing_sorted[0][1]
                    new_pos = first_pos / 2 if first_pos > 0 else -1000.0
                elif insert_idx >= len(existing_sorted):
                    # Insert at end - add gap after last
                    last_pos = existing_sorted[-1][1]
                    new_pos = last_pos + 1000.0
                else:
                    # Insert between two tasks - midpoint
                    prev_pos = existing_sorted[insert_idx - 1][1]
                    next_pos = existing_sorted[insert_idx][1]
                    new_pos = (prev_pos + next_pos) / 2

                try:
                    _set_view_position_impl(created_task["id"], view_id, new_pos)
                except Exception as e:
                    result["errors"].append(f"Failed to set position for task {created_task['id']}: {str(e)}")

                # Insert into existing_sorted for subsequent calculations
                existing_sorted.insert(insert_idx, (new_key, new_pos))
                existing_keys.insert(insert_idx, new_key)

    return result


def _setup_project_impl(
    project_id: int,
    buckets: list[str] = None,
    labels: list[dict] = None,
    tasks: list[dict] = None
) -> dict:
    """
    Set up a project with buckets, labels, and tasks in one operation.

    labels schema: [{"name": str, "color": str}]
    tasks schema: same as batch_create_tasks
    """
    buckets = buckets or []
    labels = labels or []
    tasks = tasks or []

    result = {
        "buckets_created": [],
        "labels_created": [],
        "tasks_result": None,
        "errors": []
    }

    # Step 1: Get kanban view
    view_id = None
    if buckets:
        try:
            view = _get_kanban_view_impl(project_id)
            view_id = view["id"]
        except Exception as e:
            result["errors"].append(f"Failed to get kanban view: {str(e)}")
            return result

    # Step 2: Create missing buckets
    if view_id and buckets:
        existing_buckets = _list_buckets_impl(project_id, view_id)
        existing_names = {b["title"] for b in existing_buckets}

        for i, bucket_name in enumerate(buckets):
            if bucket_name not in existing_names:
                try:
                    _create_bucket_impl(project_id, view_id, bucket_name, position=i)
                    result["buckets_created"].append(bucket_name)
                except Exception as e:
                    result["errors"].append(f"Failed to create bucket '{bucket_name}': {str(e)}")

    # Step 3: Create missing labels
    if labels:
        existing_labels = _list_labels_impl()
        existing_label_names = {l["title"] for l in existing_labels}

        for label in labels:
            label_name = label.get("name", "")
            if label_name and label_name not in existing_label_names:
                try:
                    _create_label_impl(label_name, label.get("color", "#3498db"))
                    result["labels_created"].append(label_name)
                except Exception as e:
                    result["errors"].append(f"Failed to create label '{label_name}': {str(e)}")

    # Step 4: Create tasks using batch_create_tasks
    if tasks:
        result["tasks_result"] = _batch_create_tasks_impl(
            project_id=project_id,
            tasks=tasks,
            create_missing_labels=False,  # already done above
            create_missing_buckets=False  # already done above
        )

    return result


@mcp.tool()
def batch_create_tasks(
    project_id: int = Field(description="ID of the project to create tasks in"),
    tasks: list[dict] = Field(description="List of task objects. Each task: {title: str (required), description: str, start_date: str (ISO, GANTT), end_date: str (ISO, GANTT), due_date: str (ISO, deadline), priority: int (0-5), labels: list[str], bucket: str, ref: str, blocked_by: list[str], blocks: list[str], subtask_of: str}"),
    create_missing_labels: bool = Field(default=True, description="Auto-create labels that don't exist"),
    create_missing_buckets: bool = Field(default=False, description="Auto-create buckets that don't exist"),
    use_project_config: bool = Field(default=True, description="Apply default_bucket from project config"),
    apply_sort: bool = Field(default=True, description="Auto-position tasks based on sort_strategy in project config"),
    apply_default_labels: bool = Field(default=False, description="Apply default_labels from config to tasks without labels (opt-in)")
) -> dict:
    """
    Create multiple tasks at once with labels, relations, and bucket positions.

    Reduces API calls by batching operations. Use 'ref' field to create relations
    between tasks in the same batch. Labels are matched by name (case-sensitive).

    If use_project_config=True, applies default_bucket from config.
    If apply_default_labels=True, applies default_labels from config to tasks without labels.
    If apply_sort=True, auto-positions tasks based on sort_strategy (start_date, due_date, etc.).

    GANTT VISIBILITY: Vikunja Gantt shows DAILY resolution only. For visible bars,
    use full-day spans: start_date="YYYY-MM-DDT00:00:00Z", end_date="YYYY-MM-DDT23:59:00Z".
    Put actual times in task title (e.g., "Bake pie (2pm-4pm)").

    Example:
    tasks=[
        {"title": "Design API (morning)", "ref": "design", "start_date": "2025-01-15T00:00:00Z", "end_date": "2025-01-15T23:59:00Z"},
        {"title": "Implement API", "ref": "impl", "blocked_by": ["design"]},
    ]

    Returns: {created: int, tasks: [{ref, id, title}], labels_created: [], relations_created: int, errors: []}
    """
    return _batch_create_tasks_impl(project_id, tasks, create_missing_labels, create_missing_buckets, use_project_config, apply_sort, apply_default_labels)


@mcp.tool()
def setup_project(
    project_id: int = Field(description="ID of the project to set up"),
    buckets: list[str] = Field(default=[], description="Bucket names to ensure exist (created in order)"),
    labels: list[dict] = Field(default=[], description="Labels to ensure exist: [{name: str, color: str}]"),
    tasks: list[dict] = Field(default=[], description="Tasks to create (same schema as batch_create_tasks)")
) -> dict:
    """
    Set up a project with kanban buckets, labels, and tasks in one operation.

    Higher-level tool that orchestrates bucket creation, label creation, and
    batch task creation. Use this to bootstrap a new project structure.

    Example:
    setup_project(
        project_id=1,
        buckets=["Backlog", "In Progress", "Done"],
        labels=[{"name": "bug", "color": "#e74c3c"}, {"name": "feature", "color": "#3498db"}],
        tasks=[{"title": "First task", "bucket": "Backlog", "labels": ["feature"]}]
    )

    Returns: {buckets_created: [], labels_created: [], tasks_result: {...}, errors: []}
    """
    return _setup_project_impl(project_id, buckets, labels, tasks)


def _batch_update_tasks_impl(updates: list[dict]) -> dict:
    """
    Update multiple tasks at once.

    Each update dict must have 'task_id' and any fields to update:
    title, description, start_date, end_date, due_date, priority, reminders
    """
    result = {
        "updated": 0,
        "tasks": [],
        "errors": []
    }

    for update in updates:
        task_id = update.get("task_id")
        if not task_id:
            result["errors"].append("Update missing task_id")
            continue

        try:
            # GET current task to preserve fields
            current = _request("GET", f"/api/v1/tasks/{task_id}")

            # Apply updates
            if "title" in update:
                current["title"] = update["title"]
            if "description" in update:
                current["description"] = update["description"]
            if "start_date" in update:
                current["start_date"] = update["start_date"]
            if "end_date" in update:
                current["end_date"] = update["end_date"]
            if "due_date" in update:
                current["due_date"] = update["due_date"]
            if "priority" in update:
                current["priority"] = update["priority"]
            if "reminders" in update:
                current["reminders"] = [_format_reminder_input(r) for r in update["reminders"]]

            # POST updated task
            response = _request("POST", f"/api/v1/tasks/{task_id}", json=current)
            result["updated"] += 1
            result["tasks"].append({
                "id": task_id,
                "title": response.get("title", "")
            })
        except Exception as e:
            result["errors"].append(f"Failed to update task {task_id}: {str(e)}")

    return result


@mcp.tool()
def batch_update_tasks(
    updates: list[dict] = Field(description="List of updates. Each: {task_id: int (required), title: str, description: str, start_date: str, end_date: str, due_date: str, priority: int, reminders: list[str]}")
) -> dict:
    """
    Update multiple tasks at once.

    Saves round trips when renaming multiple tasks or setting reminders on several tasks.
    Each update must include task_id and any fields to change.

    Example:
    updates=[
        {"task_id": 123, "title": "New title", "priority": 3},
        {"task_id": 456, "reminders": ["2025-12-20T10:00:00Z"]},
        {"task_id": 789, "due_date": "2025-12-25T17:00:00Z"}
    ]

    Returns: {updated: int, tasks: [{id, title}], errors: []}
    """
    return _batch_update_tasks_impl(updates)


def _batch_set_positions_impl(view_id: int, positions: list[dict]) -> dict:
    """
    Set positions for multiple tasks in a view.

    positions: [{task_id: int, position: float}, ...]
    """
    result = {
        "updated": 0,
        "tasks": [],
        "errors": []
    }

    for pos in positions:
        task_id = pos.get("task_id")
        position = pos.get("position")

        if not task_id:
            result["errors"].append("Position entry missing task_id")
            continue
        if position is None:
            result["errors"].append(f"Position entry for task {task_id} missing position")
            continue

        try:
            _set_view_position_impl(task_id, view_id, position)
            result["updated"] += 1
            result["tasks"].append({"task_id": task_id, "position": position})
        except Exception as e:
            result["errors"].append(f"Failed to set position for task {task_id}: {str(e)}")

    return result


@mcp.tool()
def batch_set_positions(
    view_id: int = Field(description="ID of the view (get from get_kanban_view)"),
    positions: list[dict] = Field(description="List of {task_id: int, position: float}")
) -> dict:
    """
    Set positions for multiple tasks in one call.

    More efficient than calling set_view_position for each task when reordering.

    Example:
    positions=[
        {"task_id": 123, "position": 1000},
        {"task_id": 456, "position": 2000},
        {"task_id": 789, "position": 3000}
    ]

    Returns: {updated: int, tasks: [{task_id, position}], errors: []}
    """
    return _batch_set_positions_impl(view_id, positions)


def _sort_bucket_impl(project_id: int, view_id: int, bucket_id: int) -> dict:
    """
    Re-sort all tasks in a bucket according to configured sort strategy.

    Fetches all tasks in bucket, sorts by strategy, assigns new positions with gaps.
    """
    result = {
        "sorted": 0,
        "tasks": [],
        "strategy": "manual",
        "errors": []
    }

    # Get project config for sort strategy
    config_result = _get_project_config_impl(project_id)
    project_config = config_result.get("config")
    if not project_config:
        result["errors"].append("No project config found")
        return result

    sort_strategy = project_config.get("sort_strategy", {})
    default_strategy = sort_strategy.get("default", "manual")
    bucket_strategies = sort_strategy.get("buckets", {})

    # Get bucket name from bucket_id
    buckets = _list_buckets_impl(project_id, view_id)
    bucket_name = None
    for b in buckets:
        if b["id"] == bucket_id:
            bucket_name = b["title"]
            break

    if not bucket_name:
        result["errors"].append(f"Bucket {bucket_id} not found")
        return result

    # Get sort strategy for this bucket
    strategy = bucket_strategies.get(bucket_name, default_strategy)
    result["strategy"] = strategy

    if strategy == "manual":
        result["errors"].append("Bucket uses manual sorting - no auto-sort applied")
        return result

    # Fetch all tasks in bucket
    tasks_raw = _get_bucket_tasks_raw(project_id, view_id, bucket_id)
    if not tasks_raw:
        return result

    # Sort tasks by strategy
    sorted_tasks = sorted(tasks_raw, key=lambda t: _get_task_sort_key(t, strategy))

    # Assign new positions with gaps (1000, 2000, 3000...)
    positions = []
    for i, task in enumerate(sorted_tasks):
        position = (i + 1) * 1000.0
        positions.append({"task_id": task["id"], "position": position})

    # Apply positions in batch
    batch_result = _batch_set_positions_impl(view_id, positions)
    result["sorted"] = batch_result["updated"]
    result["tasks"] = batch_result["tasks"]
    result["errors"].extend(batch_result["errors"])

    return result


@mcp.tool()
def sort_bucket(
    project_id: int = Field(description="ID of the project"),
    view_id: int = Field(description="ID of the kanban view (get from get_kanban_view)"),
    bucket_id: int = Field(description="ID of the bucket to sort (get from list_buckets)")
) -> dict:
    """
    Re-sort all tasks in a bucket according to the configured sort strategy.

    Uses the sort_strategy from project config. If bucket has no strategy configured,
    uses the default strategy. If strategy is 'manual', no sorting is applied.

    Useful after moving tasks or fixing legacy positions.

    Returns: {sorted: int, tasks: [{task_id, position}], strategy: str, errors: []}
    """
    return _sort_bucket_impl(project_id, view_id, bucket_id)


def _move_task_to_project_impl(task_id: int, target_project_id: int) -> dict:
    """
    Move a task from its current project to a different project.

    Updates the task's project_id field.
    """
    # GET current task
    current = _request("GET", f"/api/v1/tasks/{task_id}")
    old_project_id = current.get("project_id")

    # Update project_id
    current["project_id"] = target_project_id

    # POST updated task
    response = _request("POST", f"/api/v1/tasks/{task_id}", json=current)

    return {
        "task_id": task_id,
        "title": response.get("title", ""),
        "old_project_id": old_project_id,
        "new_project_id": target_project_id,
        "moved": True
    }


@mcp.tool()
def move_task_to_project(
    task_id: int = Field(description="ID of the task to move"),
    target_project_id: int = Field(description="ID of the project to move the task to")
) -> dict:
    """
    Move a task from its current project to a different project.

    The task will be removed from any kanban bucket in the old project.
    You may need to assign it to a bucket in the new project using set_task_position.

    Returns: {task_id, title, old_project_id, new_project_id, moved: true}
    """
    return _move_task_to_project_impl(task_id, target_project_id)


def _complete_tasks_by_label_impl(project_id: int, label_filter: str) -> dict:
    """Complete all tasks matching a label filter."""
    tasks = _list_tasks_impl(project_id, include_completed=False, label_filter=label_filter)
    result = {"completed": 0, "tasks": [], "errors": []}

    for task in tasks:
        try:
            _complete_task_impl(task["id"])
            result["completed"] += 1
            result["tasks"].append({"id": task["id"], "title": task["title"]})
        except Exception as e:
            result["errors"].append(f"Failed to complete task {task['id']}: {str(e)}")

    return result


def _move_tasks_by_label_impl(project_id: int, label_filter: str, view_id: int, bucket_id: int) -> dict:
    """Move all tasks matching a label filter to a bucket."""
    tasks = _list_tasks_impl(project_id, include_completed=False, label_filter=label_filter)
    result = {"moved": 0, "tasks": [], "errors": []}

    for task in tasks:
        try:
            _set_task_position_impl(task["id"], project_id, view_id, bucket_id)
            result["moved"] += 1
            result["tasks"].append({"id": task["id"], "title": task["title"]})
        except Exception as e:
            result["errors"].append(f"Failed to move task {task['id']}: {str(e)}")

    return result


@mcp.tool()
def complete_tasks_by_label(
    project_id: int = Field(description="ID of the project"),
    label_filter: str = Field(description="Label name to match (case-insensitive partial match)")
) -> dict:
    """
    Complete all tasks matching a label.

    Marks all incomplete tasks with the matching label as done.
    Use after an event to sweep tasks: complete_tasks_by_label(pid, "Sunday Party")

    Returns: {completed: int, tasks: [{id, title}], errors: []}
    """
    return _complete_tasks_by_label_impl(project_id, label_filter)


@mcp.tool()
def move_tasks_by_label(
    project_id: int = Field(description="ID of the project"),
    label_filter: str = Field(description="Label name to match (case-insensitive partial match)"),
    view_id: int = Field(description="ID of the kanban view"),
    bucket_id: int = Field(description="ID of the target bucket")
) -> dict:
    """
    Move all tasks matching a label to a bucket.

    Moves all incomplete tasks with the matching label to the specified kanban bucket.
    Use for workflow transitions: move_tasks_by_label(pid, "Sourdough", vid, done_bucket_id)

    Returns: {moved: int, tasks: [{id, title}], errors: []}
    """
    return _move_tasks_by_label_impl(project_id, label_filter, view_id, bucket_id)


# ============================================================================
# PROJECT CONFIG TOOLS
# ============================================================================

def _get_project_config_impl(project_id: int) -> dict:
    """Get configuration for a project."""
    config = _load_config()
    project_config = config["projects"].get(str(project_id))
    return {"project_id": project_id, "config": project_config}


def _set_project_config_impl(project_id: int, project_config: dict) -> dict:
    """Set configuration for a project (replaces existing)."""
    config = _load_config()
    created = str(project_id) not in config["projects"]
    config["projects"][str(project_id)] = project_config
    _save_config(config)
    return {"project_id": project_id, "config": project_config, "created": created}


def _update_project_config_impl(project_id: int, updates: dict) -> dict:
    """Partially update configuration for a project (deep merge)."""
    config = _load_config()
    existing = config["projects"].get(str(project_id), {})
    merged = _deep_merge(existing, updates)
    config["projects"][str(project_id)] = merged
    _save_config(config)
    return {"project_id": project_id, "config": merged}


def _delete_project_config_impl(project_id: int) -> dict:
    """Delete configuration for a project."""
    config = _load_config()
    deleted = str(project_id) in config["projects"]
    if deleted:
        del config["projects"][str(project_id)]
        _save_config(config)
    return {"project_id": project_id, "deleted": deleted}


def _list_project_configs_impl() -> dict:
    """List all configured projects."""
    config = _load_config()
    projects = []
    for pid, pconfig in config["projects"].items():
        projects.append({
            "project_id": int(pid),
            "name": pconfig.get("name", f"Project {pid}")
        })
    return {"projects": projects}


def _create_from_template_impl(
    project_id: int,
    template: str,
    anchor_time: str,
    labels: list[str] = None,
    title_suffix: str = "",
    bucket: str = None
) -> dict:
    """Create tasks from a project template with a target anchor time."""
    config = _load_config()
    project_config = config["projects"].get(str(project_id))
    if not project_config:
        raise ValueError(f"No config found for project {project_id}")

    templates = project_config.get("templates", {})
    if template not in templates:
        available = list(templates.keys()) if templates else "none"
        raise ValueError(f"Template '{template}' not found. Available: {available}")

    tmpl = templates[template]
    anchor_dt = datetime.fromisoformat(anchor_time.replace("Z", "+00:00"))

    # Build task list with calculated times
    tasks = []
    template_labels = tmpl.get("default_labels", [])
    all_labels = template_labels + (labels or [])

    for task_def in tmpl.get("tasks", []):
        offset_hours = task_def.get("offset_hours", 0)
        duration_hours = task_def.get("duration_hours", 1)

        start_dt = anchor_dt + timedelta(hours=offset_hours)
        end_dt = start_dt + timedelta(hours=duration_hours)

        # Format for Gantt visibility (full day spans)
        start_date = start_dt.strftime("%Y-%m-%dT00:00:00Z")
        end_date = start_dt.strftime("%Y-%m-%dT23:59:00Z")

        title = task_def["title"]
        if title_suffix:
            title = f"{title} {title_suffix}"

        task = {
            "title": title,
            "start_date": start_date,
            "end_date": end_date,
            "labels": all_labels.copy(),
        }

        if task_def.get("ref"):
            task["ref"] = task_def["ref"]
        if task_def.get("blocked_by"):
            task["blocked_by"] = task_def["blocked_by"]
        if bucket:
            task["bucket"] = bucket

        tasks.append(task)

    # Use batch_create_tasks to create all tasks
    result = _batch_create_tasks_impl(
        project_id=project_id,
        tasks=tasks,
        create_missing_labels=True,
        create_missing_buckets=False
    )

    return result


@mcp.tool()
def get_project_config(
    project_id: int = Field(description="ID of the Vikunja project")
) -> dict:
    """
    Get configuration for a project.

    Returns project-specific settings: sort strategy, default labels/bucket, templates.
    Returns {"project_id": X, "config": null} if no config exists.
    """
    return _get_project_config_impl(project_id)


@mcp.tool()
def set_project_config(
    project_id: int = Field(description="ID of the Vikunja project"),
    config: dict = Field(description="Configuration object: {name, sort_strategy, default_labels, default_bucket, templates}")
) -> dict:
    """
    Set configuration for a project (replaces existing).

    Config schema:
    - name: Human-readable project name
    - sort_strategy: {default: "manual"|"start_date"|..., buckets: {"Bucket": "strategy"}}
    - default_labels: Labels to auto-apply to new tasks
    - default_bucket: Default bucket for new tasks
    - templates: {name: {description, anchor, default_labels, tasks: [...]}}

    Returns: {project_id, config, created: bool}
    """
    return _set_project_config_impl(project_id, config)


@mcp.tool()
def update_project_config(
    project_id: int = Field(description="ID of the Vikunja project"),
    updates: dict = Field(description="Fields to update (deep merged with existing)")
) -> dict:
    """
    Partially update configuration for a project.

    Deep merges updates with existing config. Use this to add a template
    or change a sort strategy without replacing the entire config.

    Example: {"sort_strategy": {"buckets": {"New Bucket": "start_date"}}}
    """
    return _update_project_config_impl(project_id, updates)


@mcp.tool()
def delete_project_config(
    project_id: int = Field(description="ID of the Vikunja project")
) -> dict:
    """
    Delete configuration for a project.

    Returns: {project_id, deleted: bool}
    """
    return _delete_project_config_impl(project_id)


@mcp.tool()
def list_project_configs() -> dict:
    """
    List all configured projects.

    Returns: {projects: [{project_id, name}, ...]}
    """
    return _list_project_configs_impl()


@mcp.tool()
def create_from_template(
    project_id: int = Field(description="ID of the project to create tasks in"),
    template: str = Field(description="Template name (e.g., 'sourdough')"),
    anchor_time: str = Field(description="ISO datetime for the anchor task (e.g., '2025-12-21T09:00:00Z')"),
    labels: list[str] = Field(default=[], description="Additional labels beyond template defaults"),
    title_suffix: str = Field(default="", description="Append to task titles (e.g., '(Sun party)')"),
    bucket: str = Field(default="", description="Override default bucket placement")
) -> dict:
    """
    Create tasks from a project template with a target anchor time.

    Templates define task sequences with relative timing (offset_hours from anchor).
    The anchor task is the reference point (e.g., "bake" at T+0).

    Example: create_from_template(pid, "sourdough", "2025-12-21T09:00:00Z", labels=["🌟 Sunday Party"])
    → Creates 6 tasks with times calculated backward from 9am bake time

    Returns: {created: int, tasks: [{ref, id, title, start_date}], relations_created: int}
    """
    return _create_from_template_impl(
        project_id, template, anchor_time,
        labels if labels else None,
        title_suffix,
        bucket if bucket else None
    )


# ============================================================================
# HEALTH CHECK (for Render)
# ============================================================================

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for Render monitoring."""
    return JSONResponse({"status": "ok"})


# ============================================================================
# API KEY AUTH MIDDLEWARE
# ============================================================================

class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Middleware to validate Bearer token for SSE transport."""

    # Paths that don't require auth
    PUBLIC_PATHS = {"/health"}

    async def dispatch(self, request: Request, call_next):
        # Skip auth for public paths
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)

        # Check for API key
        api_key = os.environ.get("MCP_API_KEY")

        # If no API key configured, allow all (backward compatible)
        if not api_key:
            return await call_next(request)

        # Validate Bearer token
        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {api_key}":
            return await call_next(request)

        # Also check query param for SSE clients that can't set headers
        query_key = request.query_params.get("api_key")
        if query_key == api_key:
            return await call_next(request)

        return JSONResponse(
            {"error": "unauthorized", "message": "Invalid or missing API key"},
            status_code=401
        )


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Run the MCP server."""
    import argparse
    parser = argparse.ArgumentParser(description="Vikunja MCP Server")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse"],
                        help="Transport protocol (default: stdio)")
    parser.add_argument("--port", type=int, default=8000,
                        help="Port for SSE transport (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host for SSE transport (default: 0.0.0.0)")
    args = parser.parse_args()

    if args.transport == "sse":
        import uvicorn
        from starlette.middleware import Middleware

        # Build app with auth middleware
        middleware = [Middleware(APIKeyAuthMiddleware)]
        app = mcp.http_app(transport="sse", middleware=middleware)
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
