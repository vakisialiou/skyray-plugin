"""The small channels a Send carries — render settings, the World, cameras,
lights — built on the main thread from the scene: a payload, or the reason
this scene has nothing to send there (shown in the channel's row). Lights
and cameras cover what the Objects row covers (scene_graph.data_objects).
"""

import time
from dataclasses import dataclass
from typing import Optional

import bpy

from .camera_sync import build_camera_sync
from .light_sync import build_light_sync
from .render_settings_sync import build_render_settings_sync
from .scene_graph import data_objects
from .world_sync import build_sky, sky_to_send


class SendContext:
    """What the *_sync collectors read from a context: the job's own scene
    and view layer, whatever window happens to be active."""

    def __init__(self, scene: bpy.types.Scene, view_layer: bpy.types.ViewLayer):
        self.scene = scene
        self.view_layer = view_layer


@dataclass
class Built:
    event: str
    payload: Optional[dict] = None
    skip: str = ""
    # What a sent row adds after "Sent": a part of the channel that didn't go.
    note: str = ""


def _timestamp() -> int:
    return int(time.time() * 1000)


def render(context: SendContext) -> Built:
    return Built("blender-render-settings-sync", {"timestamp": _timestamp(), **build_render_settings_sync(context)})


def world(context: SendContext) -> Built:
    """The World's color always; its sky only when the browser renders it."""
    world = context.scene.world
    if world is None:
        return Built("blender-world-sync", skip="No World")
    payload = {"timestamp": _timestamp(), "color": list(world.color)}
    sky, _problem = sky_to_send(world)
    if sky is None:
        return Built("blender-world-sync", payload, note="color only")
    sky_node, strength = sky
    payload["sky"] = build_sky(sky_node, strength, context.scene.render.engine)
    return Built("blender-world-sync", payload)


def _skip_reason(context: SendContext, scope: str, object_type: str, noun: str) -> str:
    """Why a Lights/Camera row has nothing to send: none in the scope, or only ones Skip Hidden leaves out."""
    if data_objects(context.scene, context.view_layer, scope, False, object_type):
        return f"Only {noun}s disabled in renders"
    return f"No {noun} selected" if scope == "SELECTED" else f"No {noun}s in the scene"


def lights(context: SendContext, scope: str, skip_hidden: bool) -> Built:
    found = data_objects(context.scene, context.view_layer, scope, skip_hidden, "LIGHT")
    if not found:
        return Built("blender-lighting-sync", skip=_skip_reason(context, scope, "LIGHT", "light"))
    return Built("blender-lighting-sync", {"timestamp": _timestamp(), "lights": build_light_sync(found)})


def camera(context: SendContext, scope: str, skip_hidden: bool) -> Built:
    found = data_objects(context.scene, context.view_layer, scope, skip_hidden, "CAMERA")
    if not found:
        return Built("blender-camera-sync", skip=_skip_reason(context, scope, "CAMERA", "camera"))
    return Built("blender-camera-sync", {"timestamp": _timestamp(), "cameras": build_camera_sync(found)})


def build(channel: str, context: SendContext, scope: str, skip_hidden: bool) -> Built:
    if channel == "render":
        return render(context)
    if channel == "world":
        return world(context)
    if channel == "lights":
        return lights(context, scope, skip_hidden)
    return camera(context, scope, skip_hidden)
