"""The add-on's own folder under the user config dir — outside Blender's
folders, so it outlives restarts, upgrades and every .blend — and atomic
JSON writes into it: link_journal.py's journals. Nothing about the account
or the connection is kept there.
"""

import json
import os
import sys
import tempfile

_FOLDER = "skyray"


def folder() -> str:
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", "")
        if not os.path.isabs(base):
            base = os.path.join(home, ".config")
    return os.path.join(base, _FOLDER)


def write_json(target: str, data: object) -> None:
    """Atomic: a temp file in the same folder, then os.replace; readable by
    this user only."""
    directory = os.path.dirname(target)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        if os.name == "posix":
            os.chmod(temp_path, 0o600)
        os.replace(temp_path, target)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
