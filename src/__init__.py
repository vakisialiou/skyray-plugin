bl_info = {
    "name": "Skyray",
    "author": "Skyray",
    "version": (0, 6, 1),
    "blender": (5, 2, 1),
    "location": "View3D > Sidebar > Skyray",
    "description": "Sends the current scene to your Skyray account for live preview in the browser",
    "category": "Import-Export",
}

import bpy

from . import (
    account_operators,
    camera_operators,
    connection_ui,
    icons,
    light_operators,
    operators,
    panel,
    preferences,
    project,
    project_operators,
    project_picker,
    render_settings_operators,
    runtime,
    send_job,
    send_operators,
    status_bar,
    view_header,
    web_settings,
    world_hdri_operators,
    world_operators,
)

_classes = (
    preferences.SKYRAY_Preferences,
    account_operators.SKYRAY_OT_connect,
    account_operators.SKYRAY_OT_cancel_connect,
    account_operators.SKYRAY_OT_reopen_link,
    account_operators.SKYRAY_OT_disconnect,
    account_operators.SKYRAY_OT_dev_connect,
    account_operators.SKYRAY_OT_open_preferences,
    project_operators.SKYRAY_OT_refresh_projects,
    project_operators.SKYRAY_OT_new_project,
    project_operators.SKYRAY_OT_open_project,
    project_operators.SKYRAY_OT_manage_storage,
    send_operators.SKYRAY_OT_send_all,
    send_operators.SKYRAY_OT_resend_all,
    send_operators.SKYRAY_OT_cancel_send,
    operators.SKYRAY_OT_send_scene,
    world_operators.SKYRAY_OT_send_world,
    world_hdri_operators.SKYRAY_OT_send_world_hdri,
    light_operators.SKYRAY_OT_send_lighting,
    render_settings_operators.SKYRAY_OT_send_render_settings,
    camera_operators.SKYRAY_OT_send_camera,
    connection_ui.SKYRAY_MT_account,
    panel.SKYRAY_PT_main_panel,
    status_bar.SKYRAY_PT_status,
)


def register():
    web_settings.register()
    project.register_properties()
    project_picker.register_properties()
    send_operators.register_properties()
    icons.register()
    for cls in _classes:
        bpy.utils.register_class(cls)
    runtime.register()
    status_bar.register()
    view_header.register()


def unregister():
    send_job.unregister()
    view_header.unregister()
    status_bar.unregister()
    runtime.unregister()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    icons.unregister()
    send_operators.unregister_properties()
    project_picker.unregister_properties()
    project.unregister_properties()
    web_settings.unregister()


if __name__ == "__main__":
    register()
