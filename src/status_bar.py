"""The Skyray item in Blender's status bar, always visible: the status dot
and a few words — clicking opens the setup card or project as a popover —
or, while a Send runs, its progress bar with a Cancel button."""

import bpy

from . import connection_ui, send_job, status
from .icons import dot

_NAME_MAX = 24


class SKYRAY_PT_status(bpy.types.Panel):
    bl_idname = "SKYRAY_PT_status"
    bl_label = "Skyray"
    bl_space_type = "STATUSBAR"
    bl_region_type = "HEADER"
    bl_ui_units_x = 14

    def draw(self, context):
        connection_ui.draw_status_card(self.layout, context)


def text(current: status.Status, scene) -> str:
    color, word = connection_ui.state_dot(current, scene)
    if current.state in (status.READY, status.NO_BROWSER) and color != "red":
        name = _short(current.project_name)
        if current.state == status.READY:
            return f"Skyray · {name}"
        if current.message == status.LOADING_TEXT:
            return f"Skyray · {name} · browser loading"
        return f"Skyray · {name} · open browser"
    return f"Skyray · {word}"


def _short(name: str) -> str:
    name = name or "project"
    return name if len(name) <= _NAME_MAX else name[: _NAME_MAX - 1] + "…"


def _draw(self, context):
    job = send_job.active()
    if job is not None:
        row = self.layout.row(align=True)
        bar = row.row(align=True)
        bar.ui_units_x = 12
        fraction = job.fraction()
        bar.progress(factor=fraction, type="BAR", text=f"Skyray · Sending {round(fraction * 100)}%")
        row.operator("skyray.cancel_send", text="", icon="X")
        return
    current = status.current()
    color, _ = connection_ui.state_dot(current, context.scene)
    self.layout.popover(panel=SKYRAY_PT_status.bl_idname, text=text(current, context.scene), icon_value=dot(color))


def register() -> None:
    bpy.types.STATUSBAR_HT_header.append(_draw)


def unregister() -> None:
    bpy.types.STATUSBAR_HT_header.remove(_draw)
