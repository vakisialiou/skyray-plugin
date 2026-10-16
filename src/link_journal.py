"""Link data the add-on writes into the open .blend — object ids and the
last Send's ids — kept in a journal outside the file as well, until the
file is saved. Blender has no persistent object id of its
own (an ID's session_uid lives one session), so these exist only as data in
the file: closed without saving, or after a crash, they would be gone and
the next Send would make every object new in the browser.

A journal belongs to one saved version of one file: <config>/skyray/links/
<hash of the path>.json, holding the size and mtime that version has on
disk. It records each object and scene under its name in that version — a
rename never saved reverts with the file, and whatever was created after the
last save is gone with it. Saving the file deletes the journal (the file now
holds it all); opening the same version again replays it into the file's
data; a file changed on disk in between (saved from elsewhere, replaced)
drops it instead.
"""

import hashlib
import json
import os
from typing import Iterable, Optional

import bpy

from . import user_config

_SUBFOLDER = "links"
_KINDS = ("objects", "scenes")

# The saved version this session records against; "" while there is none
# (a file never saved, or one edited before the add-on started).
_path = ""
_version: Optional[list] = None
# (kind, session_uid) → the ID's name in that version.
_saved_names: dict = {}
# kind → {name in that version: {property: value}}
_entries: dict = {kind: {} for kind in _KINDS}


def opened() -> None:
    """After a file is read: replays its journal into the data, or drops a
    journal kept for another version of it."""
    _begin(bpy.data.filepath)
    if not _path:
        return
    stored = _read()
    if stored is None:
        return
    if stored.get("path") != _path or stored.get("version") != _version:
        _delete()
        return
    for kind in _KINDS:
        entries = stored.get(kind)
        if isinstance(entries, dict):
            _entries[kind] = {name: values for name, values in entries.items() if isinstance(values, dict)}
    _replay()


def started() -> None:
    """The add-on starts with a file open: as after reading it, unless it was
    edited since — then its data no longer is the saved version."""
    try:
        dirty = bpy.data.is_dirty
    except AttributeError:
        return  # Blender is starting up: load_post brings the file
    if dirty:
        _begin("")
    else:
        opened()


def saved(filepath: str) -> None:
    """After a save. The current file on disk now holds everything its
    journal kept, so the journal goes and a new version begins. A copy (Save
    Copy) leaves the current file, and its journal, as they were."""
    if not filepath or filepath != bpy.data.filepath:
        return
    if _path == filepath:
        _delete()
    _begin(filepath)


def record(values: Iterable[tuple]) -> None:
    """(object or scene, property, value) just written into the file's data."""
    if not _path:
        return
    changed = False
    for id_block, key, value in values:
        kind = "scenes" if isinstance(id_block, bpy.types.Scene) else "objects"
        name = _saved_names.get((kind, id_block.session_uid))
        if name is None:
            continue  # created after the last save: an unsaved close takes it too
        entry = _entries[kind].setdefault(name, {})
        if entry.get(key) != value:
            entry[key] = value
            changed = True
    if changed:
        _write()


def _collections() -> tuple:
    return (("objects", bpy.data.objects), ("scenes", bpy.data.scenes))


def _begin(filepath: str) -> None:
    global _path, _version, _saved_names, _entries
    _entries = {kind: {} for kind in _KINDS}
    _saved_names = {}
    _version = _stat(filepath) if filepath else None
    _path = filepath if _version is not None else ""
    if not _path:
        return
    for kind, collection in _collections():
        for id_block in collection:
            if id_block.library is None:
                _saved_names[(kind, id_block.session_uid)] = id_block.name


def _replay() -> None:
    for kind, collection in _collections():
        local = {id_block.name: id_block for id_block in collection if id_block.library is None}
        for name, values in _entries[kind].items():
            id_block = local.get(name)
            if id_block is None:
                continue
            for key, value in values.items():
                if key in id_block.bl_rna.properties:
                    # A bpy.props property: kept apart from custom properties.
                    setattr(id_block, key, value)
                else:
                    id_block[key] = value


def _stat(filepath: str) -> Optional[list]:
    try:
        info = os.stat(filepath)
    except OSError:
        return None
    return [info.st_size, info.st_mtime_ns]


def _file() -> str:
    name = hashlib.sha1(_path.encode("utf-8")).hexdigest()
    return os.path.join(user_config.folder(), _SUBFOLDER, f"{name}.json")


def _read() -> Optional[dict]:
    try:
        with open(_file(), encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        print(f"Skyray: ignoring unreadable {_file()}: {error}")
        return None
    return data if isinstance(data, dict) else None


def _write() -> None:
    try:
        user_config.write_json(_file(), {"path": _path, "version": _version, **_entries})
    except OSError as error:
        print(f"Skyray: couldn't write {_file()}: {error}")


def _delete() -> None:
    try:
        os.unlink(_file())
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f"Skyray: couldn't delete {_file()}: {error}")
