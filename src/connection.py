"""The link to the Skyray server. A daemon worker thread does the device
pairing and the heartbeat every few seconds. A Send hands its progress to
that heartbeat (mark_sending); the one-off calls (the sync upload, Choose's
project list, New project, the dev token) borrow the token via session().

The device token lives in memory for this Blender session only, never on
disk: closing Blender (or disabling the add-on, or switching servers)
revokes it on the server, best effort, and the next session connects anew.
SKYRAY_TOKEN (headless/CI) is used instead of pairing; it is never revoked
on exit, only by Disconnect.

The worker never touches bpy. The main thread (runtime.py) hands it the
active scene's context and reads its results through one lock; a wake event
makes it beat at once when the context or a send's progress changed.
"""

import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from . import api_client, pairing, status
from .constants import CLIENT_KIND, PLUGIN_VERSION, PROTOCOL_VERSION

# One per Blender process: two windows on one device stay apart in the
# browser's editor list.
INSTANCE_ID = uuid.uuid4().hex

_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)  # retries while the server is unreachable
_HEARTBEAT_S = 3.0  # until the server names its own interval
_OUTDATED_RETRY_S = 60.0
_DEV_RETRY_S = 30.0
_IDLE_S = 5.0
_PROGRESS_BEAT_S = 1.0  # at most one extra heartbeat per second for a send's progress
_TIMEOUT_S = 10.0
_ANNOUNCE_TIMEOUT_S = 5.0
_LEAVE_TIMEOUT_S = 2.0
_EXIT_TIMEOUT_S = 2.0  # all farewells together while Blender closes
_TEXT_MAX = 200  # the server's limit for file and scene names
_ID_MAX = 64

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

_ENV_TOKEN = "SKYRAY_TOKEN"
# Set once the server rejected SKYRAY_TOKEN (or Disconnect dropped it): the process stops offering it.
_env_rejected = False


def is_local(server_url: str) -> bool:
    try:
        return urlsplit(server_url).hostname in _LOCAL_HOSTS
    except ValueError:
        return False


@dataclass(frozen=True)
class Snapshot:
    """The worker's results, copied out for status.evaluate()."""

    server_url: str
    connected: bool
    email: str
    device_name: str
    pairing: bool
    user_code: str
    verification_url: str
    notice: str  # why the device is not connected, if there is a reason
    reachable: Optional[bool]  # None until the first heartbeat answered
    error: str  # the server's message when it answered with an error
    outdated: bool
    beat: Optional[dict]  # the last heartbeat answer
    beat_project_id: Optional[str]  # the projectId that heartbeat carried
    dev_available: bool


@dataclass(frozen=True)
class Session:
    server_url: str
    token: str
    generation: int  # pass back to revoked()/note_viewers() so a stale answer is dropped


class _Attempt:
    """One Connect click; replaced, never reused, by Cancel or a new Connect."""

    def __init__(self, name: str):
        self.name = name
        self.grant: Optional[pairing.Grant] = None
        self.next_poll = 0.0
        self.failures = 0


class Connection:
    def __init__(self, app_version: str):
        self._app_version = app_version[:40]
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[threading.Event] = None
        # Inputs from the main thread.
        self._server_url = ""
        self._online = False
        self._context = {"projectId": None, "file": "Untitled", "scene": ""}
        self._sending: Optional[dict] = None
        self._progress_beat_at = 0.0
        # The device token in use ({deviceToken, deviceId, deviceName, email,
        # source: 'session' | 'env'}) and its generation: a result computed
        # under an older generation is dropped.
        self._entry: Optional[dict] = None
        self._generation = 0
        # Pending work.
        self._attempt: Optional[_Attempt] = None
        self._revocations: list = []
        self._beat_requested = False
        self._beat_due = 0.0
        self._failures = 0
        self._dev_wanted = False
        self._dev_retry_at = 0.0
        # Results.
        self._notice = ""
        self._reachable: Optional[bool] = None
        self._error = ""
        self._outdated = False
        self._beat: Optional[dict] = None
        self._beat_project_id: Optional[str] = None
        self._dev_available: Optional[bool] = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,), name="skyray-connection", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stops the worker as Blender closes or the add-on goes. This
        session's token dies with it, so it is revoked on the server, and a
        Disconnect still queued goes first (≤2 s in all); SKYRAY_TOKEN only
        leaves the editor list."""
        with self._lock:
            thread, stop = self._thread, self._stop
            self._thread = self._stop = None
            server, entry, online = self._server_url, self._entry, self._online
            revocations, self._revocations = self._revocations, []
            self._entry = None
            self._attempt = None
        if stop is not None:
            stop.set()
            self._wake.set()
        farewells = []
        if online:
            farewells = [(self._revoke_remotely, *revocation) for revocation in revocations]
            if entry is not None:
                farewell = self._leave if entry.get("source") == "env" else self._revoke_remotely
                farewells.append((farewell, server, entry["deviceToken"]))
        if farewells:
            # Their own thread, all of them given 2 s: closing Blender never waits longer on the network.
            sender = threading.Thread(target=_send_farewells, args=(farewells,), name="skyray-farewell", daemon=True)
            sender.start()
            sender.join(timeout=_EXIT_TIMEOUT_S)
        if thread is not None:
            thread.join(timeout=0.5)

    def needs_thread(self) -> bool:
        with self._lock:
            return bool(
                self._entry
                or self._attempt
                or self._revocations
                or self._dev_wanted
            )

    # ── Inputs (main thread) ─────────────────────────────────────────

    def configure(self, server_url: str) -> None:
        """The server to use: at start, and whenever it changes. A token of
        the server before is revoked there; SKYRAY_TOKEN, when set, is used
        at once."""
        with self._lock:
            if server_url == self._server_url:
                return
            if self._entry is not None and self._entry.get("source") != "env":
                self._revocations.append((self._server_url, self._entry["deviceToken"]))
            self._server_url = server_url
            self._entry = _env_entry()
            self._reset_results()
            self._attempt = None
            self._notice = ""
            self._dev_available = None
            self._dev_retry_at = 0.0
        self._kick()

    def set_online(self, online: bool) -> None:
        with self._lock:
            if online == self._online:
                return
            self._online = online
            self._beat_requested = True
        self._wake.set()

    def set_context(self, project_id: Optional[str], file: str, scene: str) -> None:
        context = {
            "projectId": project_id[:_ID_MAX] if project_id else None,
            "file": file[:_TEXT_MAX],
            "scene": scene[:_TEXT_MAX],
        }
        with self._lock:
            if context == self._context:
                return
            self._context = context
            self._beat_requested = True
        self._wake.set()

    def request_beat(self) -> None:
        with self._lock:
            self._beat_requested = True
        self._wake.set()

    def want_dev_check(self) -> None:
        """Asks a local server whether it offers the dev shortcut (once per server)."""
        with self._lock:
            if self._entry is not None or self._dev_available is not None or self._dev_wanted:
                return
            if not is_local(self._server_url) or time.monotonic() < self._dev_retry_at:
                return
            self._dev_wanted = True
        self._kick()

    def start_pairing(self, name: str) -> None:
        with self._lock:
            if self._entry is not None:
                return
            self._attempt = _Attempt(name)
            self._notice = ""
        self._kick()

    def cancel_pairing(self) -> None:
        with self._lock:
            self._attempt = None

    def disconnect(self) -> None:
        """Forgets the device now; the worker revokes it server-side."""
        with self._lock:
            entry, server = self._entry, self._server_url
            self._attempt = None
            if entry is None:
                return
            self._revocations.append((server, entry["deviceToken"]))
            self._entry = None
            self._notice = ""
            self._reset_results()
            _drop(entry)
        self._kick()

    def adopt(self, entry: dict) -> None:
        """Uses a freshly issued device token (dev shortcut) for this session."""
        with self._lock:
            self._attempt = None
            self._entry = entry
            self._notice = ""
            self._reset_results()
        self._kick()

    # ── Results (main thread) ────────────────────────────────────────

    def snapshot(self) -> Snapshot:
        with self._lock:
            entry = self._entry or {}
            beat = self._beat or {}
            grant = self._attempt.grant if self._attempt else None
            return Snapshot(
                server_url=self._server_url,
                connected=self._entry is not None,
                email=_field(beat, "user", "email") or entry.get("email", ""),
                device_name=_field(beat, "device", "name") or entry.get("deviceName", ""),
                pairing=self._attempt is not None,
                user_code=grant.user_code if grant else "",
                verification_url=grant.verification_url if grant else "",
                notice=self._notice,
                reachable=self._reachable,
                error=self._error,
                outdated=self._outdated,
                beat=self._beat,
                beat_project_id=self._beat_project_id,
                dev_available=bool(self._dev_available),
            )

    def connected(self) -> bool:
        with self._lock:
            return self._entry is not None

    def pairing(self) -> bool:
        with self._lock:
            return self._attempt is not None

    # ── Calls on the main thread ─────────────────────────────────────

    def mark_sending(self, objects: int, sent: int, percent: int) -> None:
        """A Send's progress: its objects, how many are in the browsers, and
        the whole Send in percent. The worker announces it at once when the
        Send starts, then at most once a second."""
        with self._lock:
            starting = self._sending is None
            self._sending = {
                "objects": max(0, int(objects)),
                "sent": max(0, int(sent)),
                "percent": min(100, max(0, int(percent))),
            }
            now = time.monotonic()
            if not starting and now - self._progress_beat_at < _PROGRESS_BEAT_S:
                return
            self._progress_beat_at = now
            self._beat_requested = True
        self._wake.set()

    def end_sending(self) -> None:
        with self._lock:
            if self._sending is None:
                return
            self._sending = None
            self._beat_requested = True
        self._wake.set()

    def session(self) -> Optional["Session"]:
        """The device token in use, for a call made outside the worker; None when not connected."""
        with self._lock:
            token = self._token()
            return Session(self._server_url, token, self._generation) if token else None

    def server_url(self) -> str:
        with self._lock:
            return self._server_url

    def revoked(self, generation: int) -> None:
        """Any 401: the device was disconnected in the browser (or is unknown)."""
        with self._lock:
            if generation != self._generation or self._entry is None:
                return
            _drop(self._entry)
            self._entry = None
            self._notice = status.REVOKED_TEXT
            self._reset_results()

    def note_viewers(self, generation: int, viewers: int) -> None:
        """A sync answer's viewer count, shown before the next heartbeat."""
        with self._lock:
            if generation == self._generation and self._beat is not None:
                self._beat = {**self._beat, "viewers": viewers}

    # ── Worker ───────────────────────────────────────────────────────

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                delay = self._step()
            except Exception:
                traceback.print_exc()
                delay = _IDLE_S
            self._wake.wait(delay)
            self._wake.clear()

    def _step(self) -> float:
        """One pass over the pending work; returns the seconds until the next."""
        now = time.monotonic()
        with self._lock:
            if not self._online:
                # Online access is off: no network at all, pending revocations included.
                self._revocations = []
                return _IDLE_S
            server = self._server_url
            revocations, self._revocations = self._revocations, []
            attempt = self._attempt
            token, generation = self._token(), self._generation
            beat = token is not None and (self._beat_requested or now >= self._beat_due)
            if beat:
                self._beat_requested = False
            dev_check = token is None and self._dev_wanted
            self._dev_wanted = self._dev_wanted and not dev_check

        for revoked_server, revoked_token in revocations:
            self._revoke_remotely(revoked_server, revoked_token)
        delays = [_IDLE_S]
        if attempt is not None:
            delays.append(self._pair(attempt, server))
        if beat:
            self._heartbeat(server, token, generation, _TIMEOUT_S)
        if dev_check:
            self._check_dev(server)
        with self._lock:
            if self._token() is not None:
                delays.append(self._beat_due - time.monotonic())
        return max(0.05, min(delays))

    def _pair(self, attempt: _Attempt, server: str) -> float:
        if attempt.grant is None:
            try:
                grant = pairing.request_code(server, attempt.name, self._app_version)
            except pairing.PairingError as error:
                self._end_attempt(attempt, str(error))
                return _IDLE_S
            with self._lock:
                if self._attempt is attempt:
                    attempt.grant = grant
                    attempt.next_poll = time.monotonic() + grant.interval
            return grant.interval

        now = time.monotonic()
        if now >= attempt.grant.expires_at:
            self._end_attempt(attempt, pairing.EXPIRED)
            return _IDLE_S
        if now < attempt.next_poll:
            return attempt.next_poll - now

        result = pairing.poll_token(server, attempt.grant, _backoff(attempt.failures + 1))
        if result.error:
            self._end_attempt(attempt, result.error)
            return _IDLE_S
        if not result.token:
            attempt.failures = attempt.failures + 1 if result.offline else 0
            attempt.next_poll = time.monotonic() + result.wait
            return result.wait

        entry = session_entry(result.token, result.device)
        with self._lock:
            current = self._attempt is attempt and server == self._server_url
            if current:
                self._attempt = None
                self._entry = entry
                self._notice = ""
                self._reset_results()
        if not current:
            # Cancelled while the browser approved: don't leave a stray device behind.
            self._revoke_remotely(server, result.token)
        return 0.05

    def _end_attempt(self, attempt: _Attempt, notice: str) -> None:
        with self._lock:
            if self._attempt is attempt:
                self._attempt = None
                self._notice = notice

    def _heartbeat(self, server: str, token: str, generation: int, timeout: float) -> tuple:
        """(HTTP status, body), or (None, the transport error) — results applied under the lock."""
        with self._lock:
            context, sending = dict(self._context), self._sending
        body = {
            "protocol": PROTOCOL_VERSION,
            "client": {
                "kind": CLIENT_KIND,
                "instanceId": INSTANCE_ID,
                "appVersion": self._app_version,
                "pluginVersion": PLUGIN_VERSION,
            },
            "context": context,
            "sending": sending,
        }
        try:
            code, data = api_client.request(
                "POST", f"{server}/api/editor/heartbeat", token=token, body=body, timeout=timeout
            )
        except api_client.TransportError as error:
            self._unreachable(generation, "")
            return None, str(error)

        if code == 401:
            self.revoked(generation)
            return code, data
        if code == 426:
            with self._lock:
                if generation == self._generation:
                    self._reachable, self._error, self._outdated = True, "", True
                    self._beat = None
                    self._beat_due = time.monotonic() + _OUTDATED_RETRY_S
            return code, data
        if not api_client.ok(code) or not isinstance(data, dict):
            self._unreachable(generation, api_client.error_message(code, data))
            return code, data
        with self._lock:
            if generation != self._generation:
                return code, data
            self._reachable, self._error, self._outdated, self._failures = True, "", False, 0
            self._beat, self._beat_project_id = data, context["projectId"]
            self._beat_due = time.monotonic() + _interval(data.get("heartbeatInterval"))
            self._refresh_entry(data)
        return code, data

    def _refresh_entry(self, beat: dict) -> None:
        """The account email and device name the server reported (lock held)."""
        email, name = _field(beat, "user", "email"), _field(beat, "device", "name")
        entry = self._entry
        if entry is not None and (entry.get("email") != email or entry.get("deviceName") != name):
            self._entry = {**entry, "email": email, "deviceName": name}

    def _unreachable(self, generation: int, error: str) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._reachable, self._error = False, error
            self._failures += 1
            self._beat_due = time.monotonic() + _backoff(self._failures)

    def _check_dev(self, server: str) -> None:
        if not is_local(server):
            return
        try:
            code, data = api_client.request("GET", f"{server}/api/auth/methods", timeout=_TIMEOUT_S)
        except api_client.TransportError:
            with self._lock:
                self._dev_retry_at = time.monotonic() + _DEV_RETRY_S
            return
        with self._lock:
            if server == self._server_url:
                self._dev_available = api_client.ok(code) and isinstance(data, dict) and data.get("dev") is True

    def _revoke_remotely(self, server: str, token: str, timeout: float = _ANNOUNCE_TIMEOUT_S) -> None:
        """Best effort: revokes the token server-side, which drops this
        editor from every list too, and the device from Settings → Devices."""
        try:
            api_client.request("DELETE", f"{server}/api/devices/me", token=token, timeout=timeout)
        except api_client.TransportError:
            pass

    def _leave(self, server: str, token: str) -> None:
        try:
            api_client.request(
                "POST",
                f"{server}/api/editor/leave",
                token=token,
                body={"instanceId": INSTANCE_ID},
                timeout=_LEAVE_TIMEOUT_S,
            )
        except api_client.TransportError:
            pass

    # ── Helpers (lock held) ──────────────────────────────────────────

    def _token(self) -> Optional[str]:
        return _token_of(self._entry)

    def _reset_results(self) -> None:
        """A new token (or none): everything learned with the old one is void."""
        self._generation += 1
        self._reachable = None
        self._error = ""
        self._outdated = False
        self._beat = None
        self._beat_project_id = None
        self._failures = 0
        self._beat_requested = True

    def _kick(self) -> None:
        """Wakes the worker, starting it first if there's work (call without the lock)."""
        if self.needs_thread():
            self.start()
        self._wake.set()


def _send_farewells(farewells: list) -> None:
    """(call, server, token) each: a revocation or a leave, as Blender closes."""
    for farewell, server, token in farewells:
        farewell(server, token)


def session_entry(token: str, device: object) -> dict:
    """A freshly issued token and its `device` {id, name}, for this session."""
    device = device if isinstance(device, dict) else {}
    return {
        "deviceToken": token,
        "deviceId": str(device.get("id") or ""),
        "deviceName": str(device.get("name") or ""),
        "email": "",
        "source": "session",
    }


def _env_entry() -> Optional[dict]:
    """SKYRAY_TOKEN's entry, unless the server rejected it."""
    token = os.environ.get(_ENV_TOKEN, "").strip()
    if not token or _env_rejected:
        return None
    return {"deviceToken": token, "deviceId": "", "deviceName": "", "email": "", "source": "env"}


def _drop(entry: dict) -> None:
    """An entry the server rejected or Disconnect dropped: SKYRAY_TOKEN isn't offered again."""
    global _env_rejected
    if entry.get("source") == "env":
        _env_rejected = True


def _token_of(entry: Optional[dict]) -> Optional[str]:
    return entry["deviceToken"] if entry else None


def _field(data: dict, group: str, key: str) -> str:
    value = data.get(group)
    value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, str) else ""


def _interval(value: object) -> float:
    if isinstance(value, (int, float)) and 1 <= value <= 60:
        return float(value)
    return _HEARTBEAT_S


def _backoff(failures: int) -> float:
    return _BACKOFF_S[min(max(failures, 1), len(_BACKOFF_S)) - 1]
