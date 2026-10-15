"""Exports a single Blender object via Blender's own glTF exporter —
geometry + materials only; the Send builds the transform from scene_graph.py.

export_yup=False breaks the glTF spec on purpose: only our own GLTFLoader reads
this file, and skyray-web's scene is Z-up like Blender (THREE.Object3D.DEFAULT_UP
in render.worker.ts), so there is no axis conversion anywhere in the pipeline.

export_object() is the Send's path: textures at most the scene's Max Texture,
WebP or PNG, meshopt-compressed geometry, packed by resource_pack.py into a
glb that names its textures by key. export_object_glb() is the same pipeline
as one self-contained, lossless glb, for tools (the material fidelity suite
judges the bakes, not the transport).

An object's glb holds its own geometry only: geometry-nodes and particle
instances travel as instance sets (instance_sets.py), each exporting its
source on its own.
"""

import os
import tempfile
from contextlib import contextmanager
from typing import Optional

import bpy
from mathutils import Matrix

from .bake_device import render_device
from .material_bake import (
    bake_procedural_channels,
    cleanup_baked_materials,
    cleanup_duplicate_mesh,
)
from .material_coat import apply_coat_extras, collect_coat_extras
from .material_flatten import flatten_incompatible_surfaces
from .material_thickness import apply_thickness_extras, collect_thickness_extras
from .material_volume import approximate_volume_materials
from .resource_pack import Pack, edit_glb, pack_separate
from .shader_bake import bake_size
from .texture_cap import cap_textures
from .web_settings import Snapshot

# Lossless and uncapped: what export_object_glb() uses unless told otherwise.
LOSSLESS = Snapshot(
    max_texture=0,
    bake_size=512,
    texel_density=256.0,
    hdri_width=0,
    texture_format="PNG",
    mesh_compression="NONE",
    skip_hidden=False,
)

_COMMON = {
    "use_selection": True,
    "export_apply": True,
    "export_yup": False,
    # Only the armature's assigned action; the default ACTIONS mode also
    # pulls in any bone-compatible action in the file. This mode merges what
    # it exports into one animation, named after the action (_clip_name).
    "export_animations": True,
    "export_animation_mode": "ACTIVE_ACTIONS",
    # Only the color attributes a material reads.
    "export_all_vertex_colors": False,
    # Geometry-nodes and particle instances stay out: they go as instance sets.
    "export_gn_mesh": False,
}


def _format_options(settings: Snapshot, animated: bool = False) -> dict:
    # Meshopt quantizes animation rotations to about a degree: harmless on a
    # wing, metres of jitter on anything a bone swings at a distance (a sky
    # that turns round the scene). Animated exports keep float tracks.
    return {
        "export_image_format": "WEBP" if settings.texture_format == "WEBP" else "AUTO",
        "export_image_quality": 90,
        "export_meshopt_compression_enable": settings.mesh_compression == "MESHOPT" and not animated,
    }


def _action(obj: bpy.types.Object) -> Optional[bpy.types.Action]:
    """The action `obj`'s armature plays, the one the export carries."""
    armature = _find_armature_target(obj)
    if armature is None or armature.animation_data is None:
        return None
    return armature.animation_data.action


def _animated(obj: bpy.types.Object) -> bool:
    return _action(obj) is not None


def _clip_name(obj: bpy.types.Object) -> dict:
    """The exported animation takes its action's name ("Animation" otherwise)."""
    action = _action(obj)
    return {"export_nla_strips_merged_animation_name": action.name} if action is not None else {}


def export_object(obj: bpy.types.Object, settings: Snapshot, surface_area: float = 0.0) -> Pack:
    """`obj` as the Send uploads it. `surface_area` (world m²) sizes By Object Size bakes."""
    with _prepared(obj, settings, surface_area) as complete_materials:
        with tempfile.TemporaryDirectory() as tmp_dir:
            bpy.ops.export_scene.gltf(
                filepath=os.path.join(tmp_dir, "object.gltf"),
                export_format="GLTF_SEPARATE",
                **_COMMON,
                **_clip_name(obj),
                **_format_options(settings, _animated(obj)),
            )
            return pack_separate(tmp_dir, "object.gltf", complete_materials)


def export_object_glb(obj: bpy.types.Object, settings: Optional[Snapshot] = None) -> bytes:
    """`obj` through the same pipeline, as one self-contained glb (images inside)."""
    settings = settings or LOSSLESS
    with _prepared(obj, settings, 0.0) as complete_materials:
        with tempfile.TemporaryDirectory() as tmp_dir:
            glb_path = os.path.join(tmp_dir, "object.glb")
            bpy.ops.export_scene.gltf(
                filepath=glb_path,
                export_format="GLB",
                **_COMMON,
                **_clip_name(obj),
                **_format_options(settings, _animated(obj)),
            )
            with open(glb_path, "rb") as glb_file:
                return edit_glb(glb_file.read(), complete_materials)


def _selected(view_layer: bpy.types.ViewLayer) -> list:
    # A just-removed object can linger as None until the view layer updates.
    return [selected for selected in view_layer.objects.selected if selected is not None]


def _deselect_all(view_layer: bpy.types.ViewLayer) -> None:
    for selected in _selected(view_layer):
        selected.select_set(False)


@contextmanager
def _prepared(obj: bpy.types.Object, settings: Snapshot, surface_area: float):
    """Builds `obj`'s export copy (plus skin + animation, if it has an
    Armature modifier) at the origin, preprocessed for glTF, selected alone;
    yields the pass that completes the exported glTF's materials with what
    the exporter can't write (material_coat.py, material_thickness.py);
    removes everything it made on the way out, the original selection
    restored.

    Temporary duplicates keep the originals untouched. Duplicates share
    mesh/armature data, so this stays cheap until a preprocessing step needs
    its own copy.

    The origin reset is required: a standalone export bakes the object's
    *world* transform onto the glTF node, which would double-apply on top of
    scene_graph.py's transform. The duplicate's own animation and constraints
    go too, as they evaluate over the reset; only an armature's action is
    exported.

    A skinned mesh must be exported together with its armature target, or
    the skin binding is dropped silently. The duplicate pair is reparented
    and the reset applies to the armature; the mesh keeps `obj.matrix_local`
    so the bind pose is unchanged.
    """
    view_layer = bpy.context.view_layer
    scene = bpy.context.scene
    # Snapshot before obj.copy(): the copy inherits obj's selected state and
    # would otherwise be restored as part of the original selection.
    original_selection = _selected(view_layer)
    original_active = view_layer.objects.active

    armature = _find_armature_target(obj)

    duplicate = obj.copy()
    duplicate.animation_data_clear()
    duplicate.constraints.clear()
    duplicate_armature = None
    # Each preprocessing step appends a material the moment it creates it, so
    # cleanup also covers a step that raised partway through.
    preprocessed_materials: list = []

    try:
        if armature is not None:
            duplicate_armature = armature.copy()
            duplicate_armature.parent = None
            duplicate_armature.matrix_basis = Matrix.Identity(4)
            # The scene's own collection: always in the view layer, whatever
            # collection the user has active (an excluded one can't bake).
            scene.collection.objects.link(duplicate_armature)

            for modifier in duplicate.modifiers:
                if modifier.type == "ARMATURE" and modifier.object == armature:
                    modifier.object = duplicate_armature

            duplicate.parent = duplicate_armature
            duplicate.matrix_parent_inverse = Matrix.Identity(4)
            duplicate.matrix_basis = obj.matrix_local
        else:
            duplicate.parent = None
            duplicate.matrix_basis = Matrix.Identity(4)

        scene.collection.objects.link(duplicate)

        # The exporter reads parenting from the evaluated depsgraph (tree.py's
        # construct()), which misses the in-script reparent above until the
        # view layer updates — otherwise the skin is dropped silently.
        view_layer.update()

        _deselect_all(view_layer)
        duplicate.select_set(True)
        view_layer.objects.active = duplicate

        # Needs `duplicate` as the sole selected+active object (bake requires
        # both, and rejects a selected non-mesh such as the armature). Order
        # matters: flatten first turns a non-Principled Surface (e.g. a Mix
        # Shader) into one Principled for bake to work on.
        with render_device(scene), bake_size(settings.bake_size_for(surface_area)):
            flatten_incompatible_surfaces(duplicate, preprocessed_materials)
            bake_procedural_channels(duplicate, preprocessed_materials)
            approximate_volume_materials(duplicate, preprocessed_materials)
        cap_textures(duplicate, settings.max_texture, preprocessed_materials)
        coat_extras = collect_coat_extras(duplicate)
        thickness_extras = collect_thickness_extras(duplicate)

        def complete_materials(gltf: dict) -> bool:
            coat = apply_coat_extras(gltf, coat_extras)
            thickness = apply_thickness_extras(gltf, thickness_extras)
            return coat or thickness

        if duplicate_armature is not None:
            duplicate_armature.select_set(True)

        yield complete_materials
    finally:
        cleanup_baked_materials(preprocessed_materials)
        # Read only now: any preprocessing step may have swapped
        # duplicate.data for an independent copy, even one that then raised.
        duplicate_mesh_data = duplicate.data
        bpy.data.objects.remove(duplicate)
        cleanup_duplicate_mesh(duplicate_mesh_data)
        if duplicate_armature is not None:
            bpy.data.objects.remove(duplicate_armature)
        _deselect_all(view_layer)
        for selected in original_selection:
            try:
                selected.select_set(True)
            except ReferenceError:
                pass
        view_layer.objects.active = original_active


def _find_armature_target(obj: bpy.types.Object) -> Optional[bpy.types.Object]:
    for modifier in obj.modifiers:
        if modifier.type == "ARMATURE" and modifier.object is not None:
            return modifier.object
    return None
