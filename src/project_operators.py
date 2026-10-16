"""Project buttons: choose where this session sends from the account's
projects (asked fresh on every click), create one for this .blend, open the
chosen one in the browser, manage the account's storage.
"""

import os
from urllib.parse import quote

import bpy

from . import api_client, project, runtime, send_job, status

_NAME_MAX = 80  # the server's limit
_TIMEOUT_S = 10.0
# States where the server answered for this device, so its project calls work.
_SERVER_STATES = (status.NO_PROJECT, status.NO_BROWSER, status.READY)

# The list the last Choose fetched, (id, name) newest first, and its enum
# items: Blender keeps only pointers to the strings an items callback returns.
_listed: list = []
_items: list = []


def _listed_items(_self, _context):
    return _items


class SKYRAY_OT_choose_project(bpy.types.Operator):
    bl_idname = "skyray.choose_project"
    bl_label = "Choose project"
    bl_description = "Lists this account's projects, fresh from the server: this session sends to the one you pick"
    bl_property = "project"

    project: bpy.props.EnumProperty(name="Project", items=_listed_items, options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        current = status.current()
        if current.state not in _SERVER_STATES:
            cls.poll_message_set(current.message)
            return False
        if send_job.active() is not None:
            cls.poll_message_set("A Send is running")
            return False
        return True

    def invoke(self, context, event):
        listed, error = fetch_projects()
        if listed is None:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        if not listed:
            self.report({"INFO"}, "This account has no projects yet: create one")
            return {"CANCELLED"}
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        names = dict(_listed)
        if self.project not in names:
            return {"CANCELLED"}
        project.select(self.project, names[self.project])
        runtime.refresh()
        return {"FINISHED"}


class SKYRAY_OT_new_project(bpy.types.Operator):
    bl_idname = "skyray.new_project"
    bl_label = "New project"
    bl_description = "Creates a Skyray project named after this .blend file (or the scene); this session sends to it"

    name: bpy.props.StringProperty(
        name="Name",
        description="The new project's name; empty uses the .blend file's or the scene's name",
        default="",
        options={"SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context):
        current = status.current()
        if current.state not in _SERVER_STATES:
            cls.poll_message_set(current.message)
            return False
        return True

    def execute(self, context):
        name = (self.name.strip() or default_name(context))[:_NAME_MAX]
        created, error = _create(name)
        if created is None:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        project.select(created["id"], created["name"])
        runtime.refresh()
        self.report({"INFO"}, f"Created project {created['name']}")
        return {"FINISHED"}


class SKYRAY_OT_open_project(bpy.types.Operator):
    bl_idname = "skyray.open_project"
    bl_label = "Open in browser"
    bl_description = "Opens the chosen project in the Skyray web app"

    @classmethod
    def poll(cls, context):
        current = status.current()
        return bool(current.project_id and current.web_url)

    def execute(self, context):
        current = status.current()
        runtime.open_url(f"{current.web_url}/?project={quote(current.project_id)}")
        return {"FINISHED"}


class SKYRAY_OT_manage_storage(bpy.types.Operator):
    bl_idname = "skyray.manage_storage"
    bl_label = "Manage storage…"
    bl_description = "Opens the account's storage in the Skyray web app: what takes the space, and the plan"

    @classmethod
    def poll(cls, context):
        return bool(status.current().web_url)

    def execute(self, context):
        runtime.open_url(f"{status.current().web_url}/?settings=storage")
        return {"FINISHED"}


def fetch_projects() -> tuple:
    """The account's projects, asked now: ([(id, name), …] newest first, "")
    or (None, why not). A click waits for it, so it runs on the main thread."""
    global _listed, _items
    connection = runtime.connection()
    session = connection.session()
    if session is None:
        return None, status.NOT_CONNECTED_TEXT
    try:
        code, data = api_client.request(
            "GET", f"{session.server_url}/api/projects?summary=1", token=session.token, timeout=_TIMEOUT_S
        )
    except api_client.TransportError as error:
        return None, f"Can't reach the server: {error}"
    if code == 401:
        connection.revoked(session.generation)
        return None, status.REVOKED_TEXT
    if not api_client.ok(code) or not isinstance(data, list):
        return None, api_client.error_message(code, data)
    _listed = [
        (item["id"], str(item.get("name") or "Untitled"))
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    _items = [(project_id, name, "") for project_id, name in _listed]
    return _listed, ""


def default_name(context) -> str:
    stem = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0]
    return stem or context.scene.name or "Untitled"


def _create(name: str) -> tuple:
    """POST /api/projects → ({id, name}, "") or (None, why not)."""
    connection = runtime.connection()
    session = connection.session()
    if session is None:
        return None, status.NOT_CONNECTED_TEXT
    try:
        code, data = api_client.request(
            "POST", f"{session.server_url}/api/projects", token=session.token, body={"name": name}, timeout=_TIMEOUT_S
        )
    except api_client.TransportError as error:
        return None, f"Can't reach the server: {error}"
    if code == 401:
        connection.revoked(session.generation)
        return None, status.REVOKED_TEXT
    if not api_client.ok(code) or not isinstance(data, dict) or not isinstance(data.get("id"), str):
        return None, api_client.error_message(code, data)
    return {"id": data["id"], "name": str(data.get("name") or name)}, ""
