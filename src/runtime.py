"""The main-thread side of the connection. A 0.5 s timer (plus the file
load/save handlers) hands the server, the session's project, the file and
the scene name to the worker, turns the worker's results into
status.current(), and redraws the UI only when what it shows changed. It
also opens the browser for a pairing code, keeps the project's name as the
server reports it, clears the project when the connection is gone or another
file opens, and tells link_journal.py about file loads and saves.
"""

import traceback
from typing import Optional

import bpy
from bpy.app.handlers import persistent

from . import link_journal, preferences, project, status
from .connection import Connection, is_local

_TICK_S = 0.5
_ID_MAX = 64  # the server's limit for a project id

_connection: Optional[Connection] = None
_server_url: Optional[str] = None
_opened_code = ""
_shown: Optional[tuple] = None


def connection() -> Connection:
    return _connection


def register() -> None:
    global _connection, _server_url, _opened_code, _shown
    _connection = Connection(bpy.app.version_string)
    _server_url, _opened_code, _shown = None, "", None
    project.clear()
    status.set_current(status.Status())
    bpy.types.WindowManager.skyray_redraw = bpy.props.IntProperty(options={"HIDDEN"}, update=_redrawn)
    for handlers, handler in _handlers():
        handlers.append(handler)
    link_journal.started()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=_TICK_S, persistent=True)


def unregister() -> None:
    global _connection
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for handlers, handler in _handlers():
        if handler in handlers:
            handlers.remove(handler)
    if _connection is not None:
        _connection.stop()
        _connection = None
    project.clear()
    del bpy.types.WindowManager.skyray_redraw


def refresh() -> None:
    """The timer's work, run now: after a click, and in headless scripts where no timers run."""
    try:
        _update()
    except Exception:
        traceback.print_exc()


def request_redraw() -> None:
    """Redraws every region now (a running Send's progress); see _redraw_if_changed."""
    window_manager = bpy.context.window_manager
    if window_manager is not None:
        window_manager.skyray_redraw = (window_manager.skyray_redraw + 1) % 1_000_000


def network_allowed(server_url: str) -> bool:
    """A local server is always reachable; any other needs Blender's Online Access."""
    return is_local(server_url) or bpy.app.online_access


def open_url(url: str) -> None:
    if bpy.app.background:
        print(f"Skyray: open {url}")
        return
    try:
        bpy.ops.wm.url_open(url=url)
    except RuntimeError as error:
        print(f"Skyray: couldn't open {url}: {error}")


def _tick() -> float:
    refresh()
    return _TICK_S


def _update() -> None:
    global _server_url
    if _connection is None:
        return
    server_url = preferences.server_url()
    if server_url != _server_url:
        _server_url = server_url
        _connection.configure(server_url)
    online = network_allowed(server_url)
    _connection.set_online(online)
    if not _connection.connected():
        project.clear()  # Disconnect, a revoked device, another server: choose again once connected

    scene = bpy.context.scene
    project_id = project.selected_id()[:_ID_MAX]
    file_name = bpy.path.basename(bpy.data.filepath) or "Untitled"
    _connection.set_context(project_id or None, file_name, scene.name if scene else "")
    if _connection.needs_thread():
        _connection.start()

    current = status.evaluate(_connection.snapshot(), online, project_id, project.selected_name())
    status.set_current(current)
    _open_pairing_link(current)
    project.rename(current.project_id, current.project_name)
    _redraw_if_changed(current)


def _open_pairing_link(current: status.Status) -> None:
    """Opens the approval page once per issued code."""
    global _opened_code
    if current.state != status.WAITING_APPROVAL or not current.verification_url:
        return
    if current.user_code == _opened_code:
        return
    _opened_code = current.user_code
    open_url(current.verification_url)


def _redraw_if_changed(current: status.Status) -> None:
    """The status bar is a global area Python can't reach, and a timer has no
    region to tag; writing an ID property that has an update callback sends
    NC_WINDOW, which redraws every region — the status bar and the Skyray
    panel included. Only when what the UI shows changed.
    """
    global _shown
    key = current.key()
    if key == _shown:
        return
    _shown = key
    window_manager = bpy.context.window_manager
    if window_manager is not None:
        window_manager.skyray_redraw = (window_manager.skyray_redraw + 1) % 1_000_000


def _redrawn(_self, _context) -> None:
    pass


@persistent
def _on_load(*_args) -> None:
    # The scene a running Send reads is gone.
    from . import send_job

    send_job.cancel_active("Another file was opened")
    link_journal.opened()
    project.clear()
    project.drop_legacy()
    _on_file_change()


@persistent
def _on_save(filepath="", *_args) -> None:
    link_journal.saved(filepath)
    _on_file_change()


def _on_file_change() -> None:
    if _connection is not None:
        _connection.request_beat()
    refresh()


@persistent
def _on_exit(*_args) -> None:
    from . import send_job

    send_job.cancel_active("Blender is closing")
    if _connection is not None:
        _connection.stop()


def _handlers() -> tuple:
    return (
        (bpy.app.handlers.load_post, _on_load),
        (bpy.app.handlers.save_post, _on_save),
        (bpy.app.handlers.exit_pre, _on_exit),
    )
