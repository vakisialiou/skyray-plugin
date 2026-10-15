"""One Send: every channel its button covers, run as a sequence of short
main-thread steps from a timer, so Blender keeps drawing and answering
between them. The longest step is one object's bake + export; nothing it
creates outlives the step, so an undo step taken in between never captures
temporary data. Uploads go to the uploader thread (uploader.py).

Order: the small channels first (render settings, sky, cameras, lights),
then the HDRI, then objects. For objects the job hashes each one's export
inputs (object_key.py), asks the account's store which glbs and
instance-set placements it lacks, and bakes + exports only those; copies of
one mesh share one export.

The job pauses outside Object Mode and when its scene isn't the window's;
Cancel stops it after the current step (what already went stays in the
browser). In background mode (`blender -b`) the same steps run in a loop.
"""

import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional

import bpy

from . import (
    bake_device,
    export_cache,
    instance_sets,
    mesh_memory,
    object_key,
    project,
    runtime,
    scene_graph,
    send_channels,
    status,
    web_settings,
)
from .gltf_exporter import export_object
from .resource_pack import content_key, gpu_bytes
from .sent_ids import get_previous_sent_ids, set_sent_ids
from .uploader import StorageFull, Uploader
from .world_hdri_sync import Unreadable, build_hdri_cached, describe_world_hdri_source

# Rows of the panel, top to bottom.
CHANNELS = ("objects", "lights", "camera", "world", "hdri", "render")
_SMALL = ("render", "world", "camera", "lights")

_STEP_BUDGET_S = 0.03
_REDRAW_EVERY_S = 0.1
_PRESENCE_EVERY_S = 1.0
# Above this, the after-send report warns: the browser's frame time grows
# with the number of draw calls well before triangles matter.
DRAW_CALL_WARNING = 1000


@dataclass
class Row:
    state: str = "idle"  # idle | waiting | working | sent | skipped | failed
    done: int = 0
    total: int = 0
    note: str = ""


@dataclass
class Report:
    ok: bool
    message: str
    seconds: float
    finished_at: float
    objects: int = 0
    materials: int = 0
    triangles: int = 0
    draw_calls: int = 0
    bytes_sent: int = 0
    texture_bytes: int = 0
    exported: int = 0  # objects baked + exported this Send
    unchanged: int = 0  # objects the browser already had
    rows: dict = field(default_factory=dict)
    storage_full: Optional[StorageFull] = None  # why it stopped, when the account's space refused a file


class _Stop(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SendJob:
    def __init__(self, scene, view_layer, project_id: str, channels: tuple, scope: str, force: bool):
        self.scene = scene
        self.view_layer = view_layer
        self.project_id = project_id
        self.channels = tuple(channel for channel in CHANNELS if channel in channels)
        self.scope = scope
        self.force = force
        self.settings = web_settings.snapshot(scene)
        self.rows = {channel: Row(state="waiting" if channel in self.channels else "idle") for channel in CHANNELS}
        self.stage = "Starting…"
        self.paused = ""
        self.started_at = time.monotonic()
        self.report: Optional[Report] = None
        self.uploader: Optional[Uploader] = None
        self._gen = self._work()
        self._wait: Optional[Callable[[], bool]] = None
        self._finished = False
        self._redrawn_at = 0.0
        self._presence_at = 0.0
        self._objects_total = 0  # plan entries (meshes, empties, armatures, lights…): what the Objects row counts
        self._hashed = 0
        self._prepared = 0
        self._objects_sent = 0
        self._stats = {
            "objects": 0,
            "materials": set(),
            "triangles": 0,
            "draw_calls": 0,
            "texture_bytes": 0,
            "exported": 0,
            "unchanged": 0,
        }
        self._sent_ids_update: Optional[tuple] = None

    # ── Progress ─────────────────────────────────────────────────────

    def fraction(self) -> float:
        weights = 0.0
        done = 0.0
        for channel in self.channels:
            row = self.rows[channel]
            if channel == "objects":
                total = max(1, self._objects_total)
                weight = 3.0 * total
                part = (0.1 * self._hashed + 0.6 * self._prepared + 0.3 * self._objects_sent) / total
                if row.state in ("sent", "skipped"):
                    part = 1.0
            else:
                weight = 2.0 if channel == "hdri" else 1.0
                part = 1.0 if row.state in ("sent", "skipped", "failed") else 0.0
            weights += weight
            done += weight * min(1.0, part)
        return done / weights if weights else 0.0

    def objects_progress(self) -> tuple:
        """(sent, total) objects, as the Objects row counts them."""
        return self._objects_sent, self._objects_total

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    # ── Running ──────────────────────────────────────────────────────

    def tick(self) -> Optional[float]:
        """One timer pass: as many short steps as fit the budget, or one long one."""
        if self._finished:
            return None
        started = time.monotonic()
        try:
            while True:
                self._absorb()
                if self._wait is not None:
                    if not self._wait():
                        self._refresh()
                        return 0.1
                    self._wait = None
                    self.paused = ""
                step_started = time.monotonic()
                result = next(self._gen)
                if callable(result):
                    self._wait = result
                now = time.monotonic()
                if now - step_started > _STEP_BUDGET_S or now - started > _STEP_BUDGET_S:
                    break
        except StopIteration:
            self._finish(True, "")
            return None
        except _Stop as stop:
            self._finish(False, stop.message)
            return None
        except Exception as error:  # noqa: BLE001 — report, never leave a job hanging
            traceback.print_exc()
            self._finish(False, f"Send failed: {error}")
            return None
        self._refresh()
        return 0.0

    def run_blocking(self, timeout: float = 1800.0) -> Report:
        """The same steps in a loop, for background mode."""
        deadline = time.monotonic() + timeout
        while not self._finished:
            delay = self.tick()
            if delay is None:
                break
            if time.monotonic() > deadline:
                self.cancel("Timed out")
                break
            if delay:
                time.sleep(min(delay, 0.05))
        return self.report

    def cancel(self, message: str = "Cancelled") -> None:
        if not self._finished:
            self._finish(False, message)

    def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._presence_at >= _PRESENCE_EVERY_S:
            self._presence_at = now
            runtime.connection().mark_sending(self._objects_total, self._objects_sent, round(self.fraction() * 100))
        if now - self._redrawn_at >= _REDRAW_EVERY_S:
            self._redrawn_at = now
            runtime.request_redraw()

    def _absorb(self) -> None:
        """Turns the uploader's progress into rows; its first error ends the Send."""
        if self.uploader is None:
            return
        for tag in self.uploader.take_sent():
            if tag == "objects":
                self._objects_sent += 1
                self.rows["objects"].done = self._objects_sent
            elif tag in self.rows:
                self.rows[tag].state = "sent"
        if self.uploader.error:
            raise _Stop(self.uploader.error)

    def _finish(self, ok: bool, message: str) -> None:
        self._finished = True
        try:
            self._gen.close()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        if self.uploader is not None:
            self.uploader.close()
        for row in self.rows.values():
            if row.state in ("waiting", "working"):
                row.state = "sent" if ok else "failed"
        if ok and self._sent_ids_update is not None:
            previous, deleted, updated = self._sent_ids_update
            try:
                set_sent_ids(self.scene, (previous - deleted) | updated)
            except (ReferenceError, AttributeError, RuntimeError):
                pass  # the scene went away or is linked (read-only)
        stats = self._stats
        if ok:
            message = _summary(stats["objects"], len(stats["materials"]), self.rows)
        self.report = Report(
            ok=ok,
            message=message,
            seconds=self.elapsed(),
            finished_at=time.time(),
            objects=stats["objects"],
            materials=len(stats["materials"]),
            triangles=stats["triangles"],
            draw_calls=stats["draw_calls"],
            bytes_sent=self.uploader.bytes_sent if self.uploader else 0,
            texture_bytes=stats["texture_bytes"],
            exported=stats["exported"],
            unchanged=stats["unchanged"],
            rows={channel: Row(row.state, row.done, row.total, row.note) for channel, row in self.rows.items()},
            storage_full=self.uploader.storage_full if self.uploader else None,
        )
        _finished(self)

    # ── Steps ────────────────────────────────────────────────────────

    def _window(self):
        manager = bpy.context.window_manager
        if manager is None:
            return None
        return next((window for window in manager.windows if window.scene == self.scene), None)

    def _unpaused(self) -> bool:
        """True when the next step may run; else says why in `paused`."""
        if bpy.app.background:
            return True
        if self._window() is None:
            self.paused = f"Switch back to the scene {self.scene.name} to continue"
            return False
        active = self.view_layer.objects.active
        if active is not None and active.mode != "OBJECT":
            self.paused = "Return to Object Mode to continue"
            return False
        self.paused = ""
        return True

    def _in_window(self, work: Callable):
        """Runs `work` with the job's window as context (bake/export operators need one)."""
        window = None if bpy.app.background else self._window()
        if window is None:
            return work()
        with bpy.context.temp_override(window=window):
            return work()

    def _heavy(self, work: Callable):
        """A bake/export step; a GPU failure gives the GPU up and reruns it on the CPU."""
        try:
            return self._in_window(work)
        except RuntimeError as error:
            if not (bake_device.gpu_enabled() and bake_device.is_device_error(error)):
                raise
            traceback.print_exc()
            bake_device.give_up_gpu()
            return self._in_window(work)

    def _work(self):
        connection = runtime.connection()
        session = connection.session()
        if session is None:
            raise _Stop(status.NOT_CONNECTED_TEXT)
        self.uploader = Uploader(connection, session, self.project_id)
        context = send_channels.SendContext(self.scene, self.view_layer)

        for channel in _SMALL:
            if channel not in self.channels:
                continue
            built = send_channels.build(channel, context, self.scope, self.settings.skip_hidden)
            if built.skip:
                self.rows[channel] = Row(state="skipped", note=built.skip)
                continue
            self.rows[channel] = Row(state="working", total=1, note=built.note)
            self.uploader.sync(built.event, built.payload, channel)
            yield

        if "hdri" in self.channels:
            yield from self._hdri(context)
        if "objects" in self.channels:
            yield from self._objects()

        self.stage = "Uploading…"
        yield self.uploader.idle
        self._absorb()

    def _hdri(self, context):
        if describe_world_hdri_source(context) is None:
            self.rows["hdri"] = Row(state="skipped", note="No environment image or sky in the World")
            return
        self.rows["hdri"] = Row(state="working", total=1)
        self.stage = "Preparing the HDRI…"
        yield self._unpaused
        try:
            built = self._heavy(lambda: build_hdri_cached(self.scene, self.settings.hdri_width))
        except Unreadable as error:
            # Only this row fails: no other channel depends on the HDRI.
            self.rows["hdri"] = Row(state="failed", note=str(error))
            return
        if built is None:
            self.rows["hdri"] = Row(state="skipped", note="No environment image or sky in the World")
            return
        name, data = built
        key = content_key("h", data)
        answer = self.uploader.missing([key])
        yield lambda: answer.done
        if answer.error:
            raise _Stop(answer.error)
        if key in answer.value:
            self.uploader.blob(key, "hdri", "image/vnd.radiance", data, "hdri-blob")
        self.uploader.sync(
            "blender-world-hdri-sync",
            {"timestamp": int(time.time() * 1000), "name": name, "hdri": key},
            "hdri",
        )
        yield

    def _objects(self):
        signature = self.settings.signature()
        plan = self._in_window(
            lambda: scene_graph.plan(self.scene, self.view_layer, self.scope, self.settings.skip_hidden, signature)
        )
        if not plan.entries and not plan.deletions:
            reason = "Nothing selected" if self.scope == "SELECTED" else "The scene is empty"
            self.rows["objects"] = Row(state="skipped", note=reason)
            return
        self._objects_total = len(plan.entries)
        self.rows["objects"] = Row(state="working", total=self._objects_total)
        infos: dict = {}
        by_source: dict = {}  # copies and sets of one source hash it once
        for entry in plan.entries:
            if entry.exports:
                self.stage = f"Checking {entry.name or entry.obj.name}"
                yield self._unpaused
                info = self._in_window(lambda: _key(entry, by_source, signature))
                if info is not None:
                    infos[entry.id] = info
                    self._count(info, entry.placements.count if entry.placements else 1)
            self._hashed += 1
            yield

        keys = sorted({info.key for info in infos.values()})
        placements = sorted({entry.placements.key for entry in plan.entries if entry.placements is not None})
        if self.force or not (keys or placements):
            missing = set(keys) | set(placements)
        else:
            self.stage = "Asking the browser what it already has…"
            answer = self.uploader.missing(keys + placements)
            yield lambda: answer.done
            if answer.error:
                raise _Stop(answer.error)
            missing = set(answer.value)

        for entry in plan.entries:
            info = infos.get(entry.id)
            pack = None
            if info is not None and info.key in missing:
                pack = None if self.force else export_cache.get(info.key)
                if pack is None:
                    self.stage = f"Baking and exporting {entry.name or entry.obj.name}"
                    yield self._unpaused
                    pack = self._heavy(lambda: _export(entry, self.settings, info.surface_area))
                    export_cache.put(info.key, pack)
                    self._stats["exported"] += 1
                missing.discard(info.key)  # a copy later in the plan reuses this upload
                self._stats["texture_bytes"] += gpu_bytes(pack)
            elif info is not None:
                self._stats["unchanged"] += 1
            if entry.placements is not None and entry.placements.key in missing:
                blob = entry.placements
                self.uploader.blob(blob.key, "placements", "application/octet-stream", blob.data, "placements", "gzip")
                missing.discard(blob.key)
            self.uploader.object(
                scene_graph.entry_payload(entry, info.key if info else None),
                "objects",
                glb_key=info.key if info else "",
                pack=pack,
            )
            self._stats["objects"] += 1
            self._prepared += 1
            yield

        if plan.deletions:
            self.uploader.sync(
                "blender-sync",
                {"blenderVersion": 0, "timestamp": int(time.time() * 1000), "objects": plan.deletions},
                "deletions",
            )
        self._sent_ids_update = (
            get_previous_sent_ids(self.scene),
            plan.deleted_ids,
            {entry.id for entry in plan.entries},
        )

    def _count(self, info: object_key.Info, copies: int) -> None:
        """`info`'s mesh drawn `copies` times (a set's placements), its materials once each."""
        stats = self._stats
        stats["materials"].update(name for name in info.materials if name)
        stats["triangles"] += info.triangles * copies
        stats["draw_calls"] += max(1, info.draw_calls)


def _key(entry: scene_graph.Entry, by_source: dict, signature: str) -> Optional[object_key.Info]:
    """`entry`'s glb key: its object's, a copy's or a set's source's (each
    source hashed once a Send), or a node-made mesh's (hashed while planning)."""
    group = entry.group
    if group is not None and group.source is None:
        return group.info
    obj = group.source if group is not None else entry.obj
    if obj.name_full not in by_source:
        by_source[obj.name_full] = object_key.compute(obj, bpy.context.evaluated_depsgraph_get(), signature)
    return by_source[obj.name_full]


def _export(entry: scene_graph.Entry, settings, surface_area: float):
    """`entry`'s glb: its object's, its set's source's, or a node-made mesh's."""
    group = entry.group
    if group is None:
        return export_object(entry.obj, settings, surface_area)
    if group.source is not None:
        return export_object(group.source, settings, surface_area)
    return instance_sets.export_mesh(entry.obj, group, lambda temporary: export_object(temporary, settings, surface_area))


def _summary(objects: int, materials: int, rows: dict) -> str:
    sent = [channel for channel, row in rows.items() if row.state == "sent"]
    failed = [channel for channel, row in rows.items() if row.state == "failed"]
    if "objects" in sent and objects:
        text = f"Sent {objects} object{'s' if objects != 1 else ''}"
        if materials:
            text += f", {materials} material{'s' if materials != 1 else ''}"
    elif sent:
        text = "Sent " + ", ".join(_LABELS[channel] for channel in sent)
    else:
        text = "" if failed else "Nothing to send"
    if failed:
        missed = ", ".join(_LABELS[channel] for channel in failed) + " not sent"
        text = f"{text} · {missed}" if text else missed[0].upper() + missed[1:]
    return text


_LABELS = {
    "objects": "objects",
    "lights": "lights",
    "camera": "camera",
    "world": "World",
    "hdri": "HDRI",
    "render": "render settings",
}


# ── Module state ─────────────────────────────────────────────────────

_active: Optional[SendJob] = None
_reports: dict = {}  # scene name → Report


def active() -> Optional[SendJob]:
    return _active


def report_for(scene: bpy.types.Scene) -> Optional[Report]:
    return _reports.get(scene.name_full)


def _finished(job: SendJob) -> None:
    global _active
    if _active is job:
        _active = None
    try:
        _reports[job.scene.name_full] = job.report
    except ReferenceError:
        pass
    runtime.connection().end_sending()
    runtime.request_redraw()


def can_send(operator_class, context) -> bool:
    """poll() of every Send button: READY for the operator's own scene, no Send running."""
    current = status.current()
    if current.state != status.READY:
        operator_class.poll_message_set(current.message)
        return False
    if project.get_project_id(context) != current.project_id:
        operator_class.poll_message_set(status.CHECKING_TEXT)
        return False
    if _active is not None:
        operator_class.poll_message_set("A Send is running")
        return False
    return True


def start(context, channels: tuple, scope: str, force: bool = False) -> SendJob:
    """Starts a Send of `channels` for the context's scene; in background
    mode runs it to the end before returning."""
    global _active
    job = SendJob(
        context.scene,
        context.view_layer,
        project.get_project_id(context),
        channels,
        scope,
        force,
    )
    _active = job
    project.record_binding(context.scene)
    runtime.connection().mark_sending(0, 0, 0)
    if bpy.app.background:
        job.run_blocking()
    else:
        bpy.app.timers.register(job.tick, first_interval=0.0)
    runtime.request_redraw()
    return job


def cancel_active(message: str = "Cancelled") -> None:
    if _active is not None:
        _active.cancel(message)


def unregister() -> None:
    cancel_active("Add-on disabled")
    export_cache.clear()
    mesh_memory.clear()
