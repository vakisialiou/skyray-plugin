import bpy

from . import connection_ui, send_job, send_ui, status
from .constants import ADDON_NAME
from .icons import dot, logo


class SKYRAY_PT_main_panel(bpy.types.Panel):
    """The Skyray tab of the 3D Viewport's sidebar: the step the scene is
    missing (or its project), then what it can send, one row per channel."""

    bl_idname = "SKYRAY_PT_main_panel"
    bl_label = ADDON_NAME
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Skyray"

    def draw_header(self, context):
        self.layout.label(text="", icon_value=logo())

    def draw_header_preset(self, context):
        color, word = connection_ui.state_dot(status.current(), context.scene)
        row = self.layout.row()
        row.active = color not in ("green", "blue")
        row.label(text=word, icon_value=dot(color))

    def draw(self, context):
        layout = self.layout
        current = status.current()
        job = send_job.active()
        ready = connection_ui.is_ready(current)
        if not ready:
            connection_ui.draw_setup_card(layout, context, current)
            send_ui.draw_scope(layout, context, enabled=False)
            send_ui.draw_rows(layout, context, enabled=False)
            send_ui.draw_send_all(layout, enabled=False)
        else:
            color, _ = connection_ui.state_dot(current, context.scene)
            connection_ui.draw_project_row(layout, context, color)
            row = layout.row()
            row.active = False
            row.label(text=f"Browser open · {current.email}" if current.email else "Browser open")
            if job is not None:
                send_ui.draw_progress(layout, job)
            send_ui.draw_scope(layout, context, enabled=job is None)
            send_ui.draw_rows(layout, context, enabled=job is None)
            if job is None:
                send_ui.draw_send_all(layout, enabled=True)
                send_ui.draw_report(layout, context)
        layout.separator()
        send_ui.draw_web_settings(layout, context)
