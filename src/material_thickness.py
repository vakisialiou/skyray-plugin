"""Carries a glass material's Thickness Mode, which glTF has no slot for: a
post-pass on the exported glTF JSON, like material_coat.py. A material the
exporter wrote KHR_materials_transmission for, whose Thickness Mode is Slab,
gets `extras.thicknessMode = "slab"`; Sphere, the default, goes without.
GLTFLoader puts material extras on `material.userData`.

The thickness itself never travels: the browser takes it from the size of
the mesh the glass is drawn on. A linked Material Output Thickness and
Thickness From Shadow aren't sent: the browser always sizes by the mesh.
"""

import bpy

_TRANSMISSION = "KHR_materials_transmission"


def collect_thickness_extras(duplicate: bpy.types.Object) -> dict:
    """{material name: "slab"} for every Slab slot material. Run after the
    material preprocessing, as collect_coat_extras: a bake copy keeps its
    Blender material's mode and name."""
    extras = {}
    for slot in duplicate.material_slots:
        material = slot.material
        if material is not None and material.thickness_mode == "SLAB":
            extras[material.name] = "slab"
    return extras


def apply_thickness_extras(gltf: dict, thickness_extras: dict) -> bool:
    """Writes extras.thicknessMode on the transmissive materials of a parsed
    glTF; True when anything changed."""
    changed = False
    for material in gltf.get("materials", []):
        mode = thickness_extras.get(material.get("name"))
        if mode is None or _TRANSMISSION not in material.get("extensions", {}):
            continue
        material.setdefault("extras", {})["thicknessMode"] = mode
        changed = True
    return changed
