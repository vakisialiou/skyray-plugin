"""The project this Blender session sends to, held in memory only: chosen
from the account's projects after connecting (Choose, a fresh list each
time, or New project). Opening a file and losing the connection (Disconnect,
a revoked device, Blender closing) clear it, so a .blend never names a
project. What makes a scene's objects themselves stays in the file
(object_id.py, sent_ids.py).
"""

import bpy

# An older add-on's binding, stored on scenes; dropped as its file opens.
_LEGACY = ("skyray_project_id", "skyray_project_name")

_id = ""
_name = ""


def selected_id() -> str:
    return _id


def selected_name() -> str:
    return _name


def select(project_id: str, name: str) -> None:
    global _id, _name
    _id, _name = project_id.strip(), name


def rename(project_id: str, name: str) -> None:
    """The server's name for the chosen project, as its heartbeat reports it."""
    global _name
    if project_id == _id and name:
        _name = name


def clear() -> None:
    select("", "")


def drop_legacy() -> None:
    """Removes an older add-on's binding from the open file's own scenes: the
    value its property kept (system storage, which outlives the property) and
    the custom property an older journal's replay sets."""
    for scene in bpy.data.scenes:
        if scene.library is not None:
            continue
        for group in (scene.bl_system_properties_get(), scene):
            if group is None:
                continue
            for key in _LEGACY:
                if key in group:
                    del group[key]
