"""Helpers for scripts run headless (`blender -b file.blend --python
script.py`). Blender runs no timers there, so these do the UI timer's work in
a loop; the connection worker thread still beats as it does in the UI.
Recipe: CLAUDE.md, Headless.
"""

import time
from typing import Callable, Optional

import bpy

from . import project, runtime, send_job, status

_POLL_S = 0.1


def bind_project(project_id: str, name: str = "") -> None:
    """Makes `project_id` this session's project, as Choose does (needs a
    connection: SKYRAY_TOKEN, or a pairing approved first)."""
    project.select(project_id, name)
    runtime.refresh()


def wait_until(predicate: Callable[[status.Status], bool], timeout: float = 30.0) -> status.Status:
    """Refreshes the status until `predicate(status)` holds or `timeout` passes; returns the last status."""
    deadline = time.monotonic() + timeout
    while True:
        runtime.refresh()
        current = status.current()
        if predicate(current) or time.monotonic() >= deadline:
            return current
        time.sleep(_POLL_S)


def wait_ready(timeout: float = 30.0) -> status.Status:
    """Waits for READY (a browser shows the session's project); returns the final status."""
    return wait_until(lambda current: current.state == status.READY, timeout)


def send(channels: tuple = send_job.CHANNELS, scope: str = "ALL", force: bool = False) -> Optional[send_job.Report]:
    """Runs a Send of `channels` to the end (background mode runs it in a
    loop) and returns its report; None when not READY."""
    runtime.refresh()
    if status.current().state != status.READY:
        return None
    return send_job.start(bpy.context, tuple(channels), scope, force).report
