"""Active World → plain-dict payload: its viewport color, and the Sky
Texture (Nishita: Single or Multiple Scattering) it renders.

`color` goes whatever the World's nodes hold: it is the World's viewport
color, the browser's Wireframe/Solid World background (5.x renders a World
through its nodes only; use_nodes is deprecated and always on). The rest of
World was audited and isn't sent, nothing in the browser applies it:
mist_settings (the Mist pass), light_settings (ambient occlusion factor and
distance), lightgroup (light group passes), probe_resolution and the sun_*
settings (EEVEE's world probe and the sun light it extracts; the browser
bakes its own sky and extracts no sun), use_eevee_finite_volume (a legacy
conversion flag), and Cycles' `cycles` sampling and `cycles_visibility` ray
visibility (a World hidden from the camera that still lights the scene has
no browser counterpart).

find_world_sky() returns only a Sky the World really renders: it walks back
from the active World Output's Surface along links as Blender evaluates them
(Cycles' add_nodes(): invalid, muted and unavailable links are dropped; a
muted node or reroute passes only its internal links, so a muted Background or
Sky passes nothing) to a Background or Emission, then through its Color graph
to the Sky. A color wired straight into Surface counts too: Blender converts
it to an Emission of Strength 1. Nodes on the way (a Mix Shader, a Gamma) are
not applied by the browser, which renders the Sky as if wired directly.
"""

import math

import bpy

from .shader_bake import find_active_output

_OUTPUT_NODE_TYPE = "ShaderNodeOutputWorld"
_SKY_NODE_TYPE = "ShaderNodeTexSky"
_VALUE_NODE_TYPE = "ShaderNodeValue"
# Both turn Color * Strength into the World's radiance (Cycles' background_setup
# and emission_setup are identical).
_EMITTER_NODE_TYPES = {"ShaderNodeBackground", "ShaderNodeEmission"}

# The two Nishita models the browser renders. PREETHAM/HOSEK_WILKIE (Blender's
# "Legacy" types) are different analytic models and aren't sent.
SUPPORTED_SKY_TYPES = {"SINGLE_SCATTERING", "MULTIPLE_SCATTERING"}
SKY_NAMES = {
    "SINGLE_SCATTERING": "Single scattering",
    "MULTIPLE_SCATTERING": "Multiple scattering",
    "PREETHAM": "Preetham",
    "HOSEK_WILKIE": "Hosek-Wilkie",
}


def _source(socket: bpy.types.NodeSocket) -> bpy.types.NodeSocket | None:
    """The output socket `socket` reads, as Blender evaluates the tree."""
    while True:
        link = next(
            (
                link
                for link in socket.links
                if link.is_valid and not link.is_muted and link.from_socket.enabled
            ),
            None,
        )
        if link is None:
            return None
        output = link.from_socket
        node = output.node
        if not node.mute and node.bl_idname != "NodeReroute":
            return output
        through = next((link for link in node.internal_links if link.to_socket == output), None)
        if through is None:
            return None
        socket = through.from_socket


def _find_sky(output: bpy.types.NodeSocket) -> bpy.types.ShaderNodeTexSky | None:
    """The first Sky Texture that is or feeds the color `output`, depth-first."""
    node = output.node
    # A shader can't feed a color: Cycles drops that link.
    if output.type == "SHADER":
        return None
    if node.bl_idname == _SKY_NODE_TYPE:
        return node
    for socket in node.inputs:
        source = _source(socket) if socket.enabled else None
        sky = _find_sky(source) if source is not None else None
        if sky is not None:
            return sky
    return None


def _find_emitted_sky(
    surface: bpy.types.NodeSocket,
) -> tuple[bpy.types.ShaderNodeTexSky, bpy.types.ShaderNode | None] | None:
    """(sky, emitter) feeding the shader input `surface`; emitter None for a
    color wired straight in."""
    output = _source(surface)
    if output is None:
        return None
    node = output.node
    if output.type != "SHADER":
        sky = _find_sky(output)
        return (sky, None) if sky is not None else None
    if node.bl_idname in _EMITTER_NODE_TYPES:
        color = _source(node.inputs["Color"])
        sky = _find_sky(color) if color is not None else None
        return (sky, node) if sky is not None else None
    # A Mix/Add Shader or a group: whichever of its shader inputs holds a sky.
    for socket in node.inputs:
        if socket.type == "SHADER" and socket.enabled:
            found = _find_emitted_sky(socket)
            if found is not None:
                return found
    return None


def find_world_sky(
    world: bpy.types.World | None,
) -> tuple[bpy.types.ShaderNodeTexSky, bpy.types.ShaderNode | None] | None:
    """(sky, emitter): the Sky Texture the World renders and the Background
    or Emission it feeds (None: wired straight into the output). None if no
    Sky reaches the active World Output."""
    if world is None or world.node_tree is None:
        return None
    output = find_active_output(world.node_tree, _OUTPUT_NODE_TYPE)
    if output is None:
        return None
    return _find_emitted_sky(output.inputs["Surface"])


def emitter_strength(emitter: bpy.types.ShaderNode | None) -> float | None:
    """The Strength Blender renders the sky at; None when a node graph other
    than a Value node drives it, which the browser can't evaluate."""
    if emitter is None:
        return 1.0
    socket = emitter.inputs["Strength"]
    source = _source(socket)
    if source is None:
        return socket.default_value
    if source.node.bl_idname == _VALUE_NODE_TYPE:
        return source.default_value
    return None


def sky_to_send(
    world: bpy.types.World,
) -> tuple[tuple[bpy.types.ShaderNodeTexSky, float] | None, str]:
    """((sky, strength), "") for a Sky the browser renders, else (None, why
    not) worded for the panel; the World's color goes either way."""
    found = find_world_sky(world)
    if found is None:
        return None, "no Sky Texture"
    sky, emitter = found
    if sky.sky_type not in SUPPORTED_SKY_TYPES:
        return None, f"{SKY_NAMES.get(sky.sky_type, sky.sky_type)} sky isn't supported"
    strength = emitter_strength(emitter)
    if strength is None:
        return None, "sky strength is driven by nodes"
    return (sky, strength), ""


def build_sky(sky: bpy.types.ShaderNodeTexSky, strength: float, engine: str) -> dict:
    """Every input the Nishita models read, for a SUPPORTED_SKY_TYPES sky.
    Not sent: `turbidity`, `ground_albedo` and `sun_direction`
    (PREETHAM/HOSEK_WILKIE only), `texture_mapping` (legacy, not in the UI),
    `color_mapping` (never applied to a sky) and a linked Vector input
    (unavailable with the sun disc on; node graphs can't be applied)."""
    return {
        "skyType": sky.sky_type,
        # Only Cycles draws the disc (node_shader_tex_sky.cc: "Sun disc not
        # available in EEVEE"), so elsewhere sun_disc changes nothing.
        "sunDisc": sky.sun_disc and engine == "CYCLES",
        # Unwrapped, as stored: the browser applies Blender's own wrap.
        "sunElevationDeg": math.degrees(sky.sun_elevation),
        "sunRotationDeg": math.degrees(sky.sun_rotation),
        "sunSizeRad": sky.sun_size,
        "sunIntensity": sky.sun_intensity,
        "airDensity": sky.air_density,
        "aerosolDensity": sky.aerosol_density,
        "ozoneDensity": sky.ozone_density,
        "altitudeM": sky.altitude,
        "strength": strength,
    }
