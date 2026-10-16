"""The connection half of the Skyray panel, shared with the status-bar
popover: the status dot and word, the setup card for whatever step is
missing (account, approval, online access, project, browser), the project
row with the account menu. Drawing never waits on the network: it reads
status.current(), and asking for the project list or the dev check only
flags work for the connection worker.
"""

import bpy

from . import icons, runtime, send_job, status
from .connection import is_local
from .constants import DEFAULT_SERVER_URL
from .project_operators import default_name
from .ui_text import alert, note, primary, ring, wrap


def state_dot(current: status.Status, scene) -> tuple:
    """(dot color, short word) for the panel header, the header button and the status bar."""
    job = send_job.active()
    if job is not None:
        return "blue", f"{round(job.fraction() * 100)}%"
    state = current.state
    if state == status.NOT_CONNECTED:
        if current.message == status.REVOKED_TEXT:
            return "red", "Disconnected"
        return "grey", "Not connected"
    if state == status.WAITING_APPROVAL:
        return "amber", "Approve"
    if state == status.ONLINE_ACCESS_OFF:
        return "amber", "Online access off"
    if state == status.CONNECTING:
        return "amber", "Connecting"
    if state in (status.OFFLINE, status.OUTDATED):
        return "red", "Offline" if state == status.OFFLINE else "Update needed"
    if state == status.NO_PROJECT:
        return "amber", "No project"
    if state == status.NO_BROWSER:
        return "amber", "Browser loading" if current.message == status.LOADING_TEXT else "Browser closed"
    report = send_job.report_for(scene) if scene is not None else None
    if report is not None and not report.ok:
        return "red", "Stopped"
    return "green", "Ready"


class SKYRAY_MT_account(bpy.types.Menu):
    bl_idname = "SKYRAY_MT_account"
    bl_label = "Skyray Account"

    def draw(self, context):
        current = status.current()
        layout = self.layout
        if current.email:
            layout.label(text=current.email, icon="USER")
        if current.device_name:
            layout.label(text=f"This computer: {current.device_name}", icon="DESKTOP")
        if current.server_url and current.server_url != DEFAULT_SERVER_URL:
            layout.label(text=f"Server: {current.server_url}", icon="URL")
        layout.separator()
        layout.operator("skyray.disconnect", icon="UNLINKED")


def draw_project_row(layout, context, dot: str) -> None:
    """Project dropdown with its status dot, Open in Browser, the account menu."""
    runtime.connection().want_projects()
    row = layout.row(align=True)
    row.prop(context.scene, "skyray_project_pick", text="", icon_value=icons.dot(dot))
    row.operator("skyray.open_project", text="", icon="URL")
    row.menu(SKYRAY_MT_account.bl_idname, text="", icon="USER")


def is_ready(current: status.Status) -> bool:
    return current.state == status.READY or send_job.active() is not None


def draw_setup_card(layout, context, current: status.Status) -> None:
    """The one card for the step the scene is missing; nothing when ready."""
    state = current.state
    box = layout.box()
    if state == status.NOT_CONNECTED:
        if current.message == status.REVOKED_TEXT:
            alert(box, "This computer was disconnected")
            wrap(box, context, "It was disconnected from your account in the browser. Connect again to keep sending.")
            primary(box, "skyray.connect", "Connect Again", "LINKED")
        elif not current.online:
            _draw_online_off(box, context)
        else:
            box.label(text="Connect your Skyray account", icon_value=icons.dot("grey"))
            wrap(box, context, "Approve this computer once in the browser. After that every scene can send to your projects.")
            primary(box, "skyray.connect", "Connect Account", "LINKED")
            if current.dev_available and is_local(current.server_url):
                dev = box.row()
                dev.active = False
                dev.operator("skyray.dev_connect", text="Dev: Connect Without Browser", emboss=False)
            elif not current.dev_available:
                runtime.connection().want_dev_check()
        return
    if state == status.WAITING_APPROVAL:
        box.label(text="Approve in your browser", icon_value=icons.dot("amber"))
        code = box.box().row()
        code.alignment = "CENTER"
        code.scale_y = 2.0
        code.label(text=current.user_code or "Requesting a code…")
        wrap(box, context, "Check that the browser shows the same code, then approve it there.")
        row = box.row(align=True)
        row.operator("skyray.reopen_link", text="Open Again", icon="URL")
        row.operator("skyray.cancel_connect", text="Cancel", icon="X")
        return
    if state == status.ONLINE_ACCESS_OFF:
        _draw_online_off(box, context)
        return
    if state == status.CONNECTING:
        ring(box, "Connecting…")
        return
    if state == status.OFFLINE:
        alert(box, "Can't reach Skyray", icon="INTERNET_OFFLINE")
        ring(box, "Retrying…", 0.65)
        if current.message and current.message != status.OFFLINE_TEXT:
            wrap(box, context, current.message)
        return
    if state == status.OUTDATED:
        alert(box, "This add-on is out of date")
        wrap(box, context, "Install the latest Skyray add-on, then restart Blender.")
        return
    if state == status.NO_PROJECT:
        box.label(text="Choose a project for this scene", icon_value=icons.dot("amber"))
        runtime.connection().want_projects()
        row = box.row(align=True)
        row.prop(context.scene, "skyray_project_pick", text="")
        row.operator("skyray.refresh_projects", text="", icon="FILE_REFRESH")
        box.operator("skyray.new_project", text=f"New Project: {default_name(context)}", icon="ADD")
        if current.message not in (status.NO_PROJECT_TEXT, status.CHECKING_TEXT):
            note(box, current.message, icon="INFO")
        if context.preferences.view.show_developer_ui:
            box.prop(context.scene, "skyray_project_id")
        return
    if state == status.NO_BROWSER:
        draw_project_row(box, context, "amber")
        if current.message == status.LOADING_TEXT:
            wrap(box, context, "The project is loading in the browser: Send opens once it shows.", icon="TIME")
            return
        wrap(box, context, "Open the project in a browser tab to send.", icon="WINDOW")
        primary(box, "skyray.open_project", "Open in Browser", "URL")


def _draw_online_off(box, context) -> None:
    box.label(text="Online access is off", icon_value=icons.dot("amber"))
    wrap(box, context, "Skyray needs Blender's online access to reach the server.")
    primary(box, "skyray.open_preferences", "Allow Online Access", "PREFERENCES")


def draw_status_card(layout, context) -> None:
    """The popover's whole content: the setup card, or the project row when ready."""
    current = status.current()
    if is_ready(current):
        dot, _ = state_dot(current, context.scene)
        draw_project_row(layout, context, dot)
        note(layout, f"Browser open · {current.email}" if current.email else "Browser open")
    else:
        draw_setup_card(layout, context, current)
