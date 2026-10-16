"""The connection status the panel, the status bar and every Send button's
poll() read. evaluate() turns the worker's snapshot plus the session's
project (project.py) into one state — the first failing check wins:

NOT_CONNECTED (WAITING_APPROVAL while pairing) → ONLINE_ACCESS_OFF →
CONNECTING / OFFLINE → OUTDATED → NO_PROJECT → NO_BROWSER → READY.

Only runtime.py's main-thread timer sets the current status; no bpy here.
"""

from dataclasses import astuple, dataclass, replace

NOT_CONNECTED = "NOT_CONNECTED"
WAITING_APPROVAL = "WAITING_APPROVAL"
ONLINE_ACCESS_OFF = "ONLINE_ACCESS_OFF"
CONNECTING = "CONNECTING"
OFFLINE = "OFFLINE"
OUTDATED = "OUTDATED"
NO_PROJECT = "NO_PROJECT"
NO_BROWSER = "NO_BROWSER"
READY = "READY"

NOT_CONNECTED_TEXT = "Not connected"
WAITING_TEXT = "Waiting for approval in the browser"
ONLINE_OFF_TEXT = "Online access is off in Preferences"
CONNECTING_TEXT = "Connecting…"
OFFLINE_TEXT = "Can't reach the server — retrying…"
OUTDATED_TEXT = "Add-on outdated — update it"
NO_PROJECT_TEXT = "No project selected"
CHECKING_TEXT = "Checking the project…"
FOREIGN_TEXT = "Project belongs to another account"
NOT_FOUND_TEXT = "Project not found"
NO_BROWSER_TEXT = "Browser not open"
# A browser has the project's scene open, its editor not shown yet: Send waits for it.
LOADING_TEXT = "Browser loading…"
REVOKED_TEXT = "Device disconnected in the browser"
NO_VIEWERS_TEXT = "Open the project in a browser first"
STORAGE_FULL_TEXT = "Send stopped — storage full"

ICONS = {
    NOT_CONNECTED: "UNLINKED",
    WAITING_APPROVAL: "TIME",
    ONLINE_ACCESS_OFF: "INTERNET_OFFLINE",
    CONNECTING: "TIME",
    OFFLINE: "INTERNET_OFFLINE",
    OUTDATED: "ERROR",
    NO_PROJECT: "ERROR",
    NO_BROWSER: "WINDOW",
    READY: "CHECKMARK",
}

# States that need an account-level fix; the rest are about the session's project.
ACCOUNT_STATES = (NOT_CONNECTED, WAITING_APPROVAL, ONLINE_ACCESS_OFF, CONNECTING, OFFLINE, OUTDATED)


@dataclass(frozen=True)
class Status:
    state: str = NOT_CONNECTED
    message: str = NOT_CONNECTED_TEXT
    connected: bool = False  # this server has a device token
    online: bool = True  # network calls allowed (see runtime.network_allowed)
    email: str = ""
    device_name: str = ""
    project_id: str = ""  # set once the server verified the session's project
    project_name: str = ""
    viewers: int = 0
    web_url: str = ""
    user_code: str = ""
    verification_url: str = ""
    dev_available: bool = False
    server_url: str = ""

    def key(self) -> tuple:
        """Everything a redraw would show; the UI redraws when it changes."""
        return astuple(self)


_current = Status()


def current() -> Status:
    return _current


def set_current(status: Status) -> None:
    global _current
    _current = status


def evaluate(snapshot, online: bool, bound_id: str, bound_name: str) -> Status:
    """`snapshot` is a connection.Snapshot; `bound_id`/`bound_name` the session's project."""
    base = Status(
        connected=snapshot.connected,
        online=online,
        email=snapshot.email,
        device_name=snapshot.device_name,
        project_name=bound_name,
        server_url=snapshot.server_url,
        dev_available=snapshot.dev_available,
    )
    if not snapshot.connected:
        if snapshot.pairing:
            return _with(
                base,
                WAITING_APPROVAL,
                WAITING_TEXT,
                user_code=snapshot.user_code,
                verification_url=snapshot.verification_url,
            )
        return _with(base, NOT_CONNECTED, snapshot.notice or NOT_CONNECTED_TEXT)
    if not online:
        return _with(base, ONLINE_ACCESS_OFF, ONLINE_OFF_TEXT)
    if snapshot.reachable is None:
        return _with(base, CONNECTING, CONNECTING_TEXT)
    if not snapshot.reachable:
        message = f"Server error: {snapshot.error} — retrying…" if snapshot.error else OFFLINE_TEXT
        return _with(base, OFFLINE, message)
    if snapshot.outdated:
        return _with(base, OUTDATED, OUTDATED_TEXT)

    beat = snapshot.beat or {}
    base = replace(base, web_url=beat.get("webUrl") if isinstance(beat.get("webUrl"), str) else "")
    if not bound_id:
        return _with(base, NO_PROJECT, NO_PROJECT_TEXT)
    if snapshot.beat_project_id != bound_id:
        return _with(base, NO_PROJECT, CHECKING_TEXT)
    project = beat.get("project")
    if beat.get("projectError") == "foreign":
        return _with(base, NO_PROJECT, FOREIGN_TEXT)
    if not isinstance(project, dict) or project.get("id") != bound_id:
        return _with(base, NO_PROJECT, NOT_FOUND_TEXT)

    viewers = beat.get("viewers") if isinstance(beat.get("viewers"), int) else 0
    # Windows whose scene still loads; a server that doesn't count them sends none.
    loading = beat.get("loading") if isinstance(beat.get("loading"), int) else 0
    verified = replace(base, project_id=bound_id, project_name=str(project.get("name") or bound_name), viewers=viewers)
    if viewers <= 0:
        return _with(verified, NO_BROWSER, LOADING_TEXT if loading > 0 else NO_BROWSER_TEXT)
    return _with(verified, READY, f"Browser open ({viewers})")


def _with(status: Status, state: str, message: str, **fields) -> Status:
    return replace(status, state=state, message=message, **fields)
