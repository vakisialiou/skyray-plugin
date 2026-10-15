import bpy

from .send_operators import SendChannelBase


class SKYRAY_OT_send_world(SendChannelBase, bpy.types.Operator):
    bl_idname = "skyray.send_world"
    bl_label = "Send World"
    bl_description = "Sends the World: its viewport color and its Sky Texture (sun position, atmosphere)"

    channels = ("world",)
