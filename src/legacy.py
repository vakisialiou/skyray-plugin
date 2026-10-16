"""What an older add-on left behind, cleaned up: its device tokens file (this
one keeps its token in memory only) and its scene binding (this one names no
project in a .blend).
"""

import json
import os
import threading

import bpy

from . import api_client, user_config
from .connection import is_local

_CREDENTIALS = "credentials.json"
_BINDING = ("skyray_project_id", "skyray_project_name")
_REVOKE_TIMEOUT_S = 5.0


def drop_credentials() -> None:
    """At start: the tokens file goes at once, and its tokens are revoked on
    their servers off the main thread, best effort, so their devices leave
    Settings → Devices."""
    path = os.path.join(user_config.folder(), _CREDENTIALS)
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        data = None
    try:
        os.unlink(path)
    except OSError as error:
        print(f"Skyray: couldn't delete {path}: {error}")
    servers = data.get("servers") if isinstance(data, dict) else None
    tokens = [
        (server.rstrip("/"), entry["deviceToken"])
        for server, entry in (servers if isinstance(servers, dict) else {}).items()
        if isinstance(entry, dict) and isinstance(entry.get("deviceToken"), str) and entry["deviceToken"]
    ]
    # Online access off: no call leaves this computer but to a local server.
    tokens = [(server, token) for server, token in tokens if is_local(server) or bpy.app.online_access]
    if tokens:
        threading.Thread(target=_revoke, args=(tokens,), name="skyray-legacy", daemon=True).start()


def drop_binding() -> None:
    """Removes an older add-on's binding from the open file's own scenes: the
    value its property kept (system storage, which outlives the property) and
    the custom property an older journal's replay sets."""
    for scene in bpy.data.scenes:
        if scene.library is not None:
            continue
        for group in (scene.bl_system_properties_get(), scene):
            if group is None:
                continue
            for key in _BINDING:
                if key in group:
                    del group[key]


def _revoke(tokens: list) -> None:
    for server, token in tokens:
        try:
            api_client.request("DELETE", f"{server}/api/devices/me", token=token, timeout=_REVOKE_TIMEOUT_S)
        except api_client.TransportError:
            pass
