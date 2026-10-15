"""Completes the Principled coat layer that Blender's glTF exporter leaves
incomplete (io_scene_gltf2's material/extensions/clearcoat.py):
KHR_materials_clearcoat carries Coat Weight, Coat Roughness and an image-fed
Coat Normal (material_bake.py bakes procedural ones first), but never Coat IOR
or Coat Tint, and omits clearcoatRoughnessFactor within 1e-5 of Blender's own
default 0.03 (BLENDER_COAT_ROUGHNESS) — glTF's default is 0.

A post-pass on the exported glTF JSON, per material that has
KHR_materials_clearcoat:
- `extras.coat = {ior, tint}` (tint scene-linear RGB), each only when its
  input is constant and not Blender's default — a procedural one isn't
  carried, the browser falls back to the default. GLTFLoader puts material
  extras on `material.userData`.
- an omitted clearcoatRoughnessFactor is written back explicitly.

Whether the coat is on, and its constants, are read the way the exporter
reads its own channels: off the material's InlineShaderNodes tree
(inlined_principled), a constant being unlinked or fed straight by an
RGB/Value node (search_node_tree's NodeNav.get_constant).

Not a glTF2ExportUserExtension hook: io_scene_gltf2 discovers those only on
enabled add-ons (and would then also change the user's own File > Export).
"""

from contextlib import contextmanager
from typing import Iterator, Optional

import bpy

from .shader_bake import find_principled_surface

_CLEARCOAT = "KHR_materials_clearcoat"

# Principled BSDF's own Coat IOR / Coat Tint defaults (node_shader_bsdf_principled.cc).
_DEFAULT_IOR = 1.5
_DEFAULT_TINT = (1.0, 1.0, 1.0)
_TOLERANCE = 1e-6
# The constant node NodeNav.get_constant accepts per input type.
_CONSTANT_NODES = {"RGBA": "ShaderNodeRGB", "VALUE": "ShaderNodeValue"}


@contextmanager
def inlined_principled(
    material: bpy.types.Material,
) -> Iterator[Optional[bpy.types.ShaderNodeBsdfPrincipled]]:
    """The Principled surface of the material's InlineShaderNodes tree (None
    without one) — what Blender renders and its glTF exporter reads: node
    groups inlined, muted links dropped, constant subgraphs folded. Read it
    only inside the `with`: the tree is freed with `inline`, and a node read
    after that crashes Blender."""
    inline = bpy.types.InlineShaderNodes.from_material(material)
    yield find_principled_surface(inline)


def has_coat(principled: bpy.types.ShaderNodeBsdfPrincipled) -> bool:
    """On an inlined Principled (inlined_principled): Coat Weight linked or
    above 0 — when the exporter writes the extension (a linked weight once
    baked). A muted link or a constant 0 is no coat."""
    weight = principled.inputs["Coat Weight"]
    return weight.is_linked or weight.default_value > 0.0


def _constant(socket: bpy.types.NodeSocket):
    """The input's value if constant (RGB as a 3-list), else None."""
    if socket.is_linked:
        link = socket.links[0]
        if link.from_node.bl_idname != _CONSTANT_NODES.get(socket.type):
            return None
        value = link.from_socket.default_value
    else:
        value = socket.default_value
    return list(value)[:3] if socket.type == "RGBA" else value


def _is_default(value, default) -> bool:
    if isinstance(value, list):
        return all(abs(a - b) <= _TOLERANCE for a, b in zip(value, default))
    return abs(value - default) <= _TOLERANCE


def _coat_layer(material: bpy.types.Material) -> Optional[dict]:
    """{"ior"?, "tint"?, "roughness"?} for a coated material, else None."""
    with inlined_principled(material) as principled:
        if principled is None or not has_coat(principled):
            return None
        coat = {}
        ior = _constant(principled.inputs["Coat IOR"])
        if ior is not None and not _is_default(ior, _DEFAULT_IOR):
            coat["ior"] = ior
        tint = _constant(principled.inputs["Coat Tint"])
        if tint is not None and not _is_default(tint, _DEFAULT_TINT):
            coat["tint"] = tint
        roughness = _constant(principled.inputs["Coat Roughness"])
        if roughness is not None:
            coat["roughness"] = roughness
        return coat


def collect_coat_extras(duplicate: bpy.types.Object) -> dict:
    """{material name: _coat_layer()} for every coated slot material. Run
    after the material preprocessing: the slots then hold the materials
    actually exported, and a glTF material keeps its Blender material's name
    (a bake copy's `.001` included)."""
    extras = {}
    for slot in duplicate.material_slots:
        material = slot.material
        if material is None or material.node_tree is None:
            continue
        coat = _coat_layer(material)
        if coat is not None:
            extras[material.name] = coat
    return extras


def apply_coat_extras(gltf: dict, coat_extras: dict) -> bool:
    """Completes the exported clearcoat in a parsed glTF (extras.coat, an
    omitted roughness); True when anything changed."""
    changed = False
    for material in gltf.get("materials", []):
        coat = coat_extras.get(material.get("name"))
        clearcoat = material.get("extensions", {}).get(_CLEARCOAT)
        if coat is None or clearcoat is None:
            continue
        layer = {key: coat[key] for key in ("ior", "tint") if key in coat}
        if layer:
            material.setdefault("extras", {})["coat"] = layer
            changed = True
        has_roughness = (
            "clearcoatRoughnessFactor" in clearcoat or "clearcoatRoughnessTexture" in clearcoat
        )
        if not has_roughness and "roughness" in coat:
            clearcoat["clearcoatRoughnessFactor"] = coat["roughness"]
            changed = True
    return changed
