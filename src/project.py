"""The project this Blender session sends to, held in memory only: chosen
from the account's projects after connecting (Choose, a fresh list each
time, or New project). Opening a file and losing the connection (Disconnect,
a revoked device, Blender closing) clear it, so a .blend never names a
project (an older add-on's binding goes as its file opens: legacy.py). What
makes a scene's objects themselves stays in the file (object_id.py,
sent_ids.py).
"""

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
