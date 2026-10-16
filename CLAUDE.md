# skyray-plugin

One Blender add-on, `skyray` (Python, `bpy`): it reads the current scene and sends it to `skyray-api` over HTTP. Cross-repo architecture, shared Key Principles and the Settings Display Model: `../CLAUDE.md` (the documentation hub). This file covers only what's unique to this repo.

## Structure

- `src/` — the add-on's code, all of it and nothing else: the whole folder ships in every zip as `skyray/`, the module name Blender installs the add-on and keys its preferences by, so it stays `skyray`. Per-file roles: `docs/file-structure.md`.
- **Builds** — `python3 build.py` writes three zips from `src/` into the repo root (gitignored), one per server:
  - `skyray-local.zip`: «Skyray (local)», `http://localhost:3500`;
  - `skyray-dev.zip`: «Skyray (dev)», `https://dev-gateway.skyray.app`;
  - `skyray-prod.zip`: «Skyray», `https://gateway.skyray.app`.

  They differ only in `bl_info["name"]` (also the panel's title) and `constants.DEFAULT_SERVER_URL`. `SKYRAY_SERVER_URL` and the developer preference still override the server. The source tree is the local build. All three are the one module `skyray`, so installing one replaces another.
- **Connection** — plain HTTP, stdlib only (`urllib`; Blender's Python has no socketio/requests), no socket at all. The heartbeat lives on the worker thread and keeps beating while the main thread bakes. Every Blender session connects anew: Connect account runs a device-code pairing approved in the browser, and the device token stays in memory only, never on disk or in a .blend. Disconnect, quitting Blender, disabling the add-on and a server change revoke it on the server (best effort; a crash leaves the device in Settings → Devices). A daemon worker thread (`connection.py`) heartbeats every 3 s and does the pairing; `runtime.py`'s 0.5 s timer feeds it the session's project and turns its results into the status (`status.py`) that drives the panel, the status-bar item, the header button and every Send's poll(). The session's project (`project.py`) is chosen after connecting, from the account's projects asked fresh on every Choose, or made with New project; opening a file or losing the connection clears it, and no .blend names one.
- **Send** — a button starts a job (`send_job.py`) and returns at once; the job runs from a timer as short main-thread steps (one object's bake + export is the longest), so Blender keeps drawing in between, and its uploads go to their own thread (`uploader.py`). Objects are named by a hash of their export inputs (`object_key.py`); the job asks the account's store which glbs it lacks (`/api/editor/missing`) and bakes + exports only those. Blobs go raw to `/api/editor/resource` — into the account's store on the server, under the account's space (a full one stops the Send with its numbers and Manage storage…) — a gzip glb naming its textures by key, each texture once, then the `/api/editor/sync` messages that name them. Web Optimization (`web_settings.py`, per scene) sets texture/bake/HDRI sizes, WebP/PNG and meshopt. Wire format: `../docs/sync-protocol.md`.
- Test and demo scenes live in the sibling repo `../skyray-assets` (Git LFS; its rules in its `CLAUDE.md`). This repo holds only the add-on's code — no `.blend` or other binaries.

## Critical Discipline

**Rebuild the zips (`python3 build.py`) immediately after any source change, and restart Blender after reinstalling — never trust a hot-reinstall** (a running session keeps old modules cached in `sys.modules`, so a stale copy silently reintroduces bugs that look like regressions; another build installed over a running one keeps the old one's server and title until the restart). Command + reinstall steps: `rebuild-plugin-zip` skill (`.claude/skills/rebuild-plugin-zip/SKILL.md`).

## Conventions

- **Send only fields the browser can actually apply and that affect the result** (`../CLAUDE.md`'s Settings Display Model). Before dropping a field as unused, confirm it's dead in *Blender's own source*, not just unread by the browser yet (e.g. `turbidity`/`ground_albedo` are unused by Blender's own `MULTIPLE_SCATTERING` sky, so `world_sync.py` doesn't send them). The camera, light, render-settings and world `*_sync.py` docstrings list what was audited and deliberately not sent — read them before adding a field.
- One `*_operators.py` + `*_sync.py` pair per sync concern — never append a new concern to an existing file.
- Verify property names/units by introspecting the live `bpy.types.*` of the running Blender version, not from memory or docs.
- **A Send is a job, never a blocking operator.** Its steps run from a timer; a step leaves nothing temporary behind (a duplicate, a material copy, a bake image), so an undo step taken between steps never captures them. Long work becomes more steps, not a longer one. A new channel adds a builder to `send_channels.py` (or its own step in `send_job.py`) and a row to `send_ui.py` — the operator stays a one-line `SendChannelBase` subclass.
- **Never call `bpy` from a thread.** The connection worker and the uploader touch only their lock-guarded state; `runtime.py` and the job (main thread) move data between them and Blender. The only main-thread network calls are the ones a click waits for anyway (Choose's project list, New project, the dev token).
- **Link data written into the .blend goes through `link_journal.record`** — object ids and the last Send's ids. Blender has no persistent object id, so the journal keeps them outside the file until the next save and replays them when the same saved version is opened again; without it an unsaved close makes every sent object new in the browser.
- **Object keys must see every export input.** A changed input `object_key.py` misses leaves a stale object in the browser until Resend Everything; an extra input only costs a re-export. A change to what the exporter writes for the same scene bumps `EXPORT_VERSION` (`constants.py`).
- **Material export**: glTF carries only flat factors or Image Textures; Blender's exporter silently *omits* any procedurally-driven input (an omitted `baseColorFactor` becomes glTF's default white) and ignores the Volume socket. So `gltf_exporter.py` preprocesses the temp duplicate's materials in order: `material_flatten.py` first (`material_bake.py` only handles a Surface that already is one Principled BSDF), then `material_bake.py`, then `material_volume.py`, then `texture_cap.py` (roles: `docs/file-structure.md`). A step swaps a slot's material through `slot.material`, which follows the slot's link (mesh or object) as the bake and the exporter do; `data.materials[i]` misses an object-linked slot. Find the Principled by walking from Material Output's Surface (`shader_bake.find_principled_surface`), never "the first Principled by type". A bake that comes out flat ships as a factor (`shader_bake.flat_value`). A Base Color that is a Color Attribute, alone or multiplied (Mix, factor 1) by a direct Image Texture, is glTF's own COLOR_0 × texture and exports without a bake (`material_bake._needs_base_color_bake`). Bakes run on the GPU when Preferences enables one, falling back to the CPU for the session if it fails (`bake_device.py`). Fidelity tests and fixture regeneration (after changing any `material_*.py`, `shader_bake.py` or `gltf_exporter.py`): `../skyray-web/test/material/README.md` — they read `export_object_glb()`, the same pipeline as one lossless glb.
- **Instances**: an object's glb holds its own geometry only (`export_gn_mesh` off). Its geometry-nodes and particle instances go as instance sets, one per source object or node-made mesh, each exporting its mesh through the usual pipeline (a node-made mesh from a temporary copy) with its placements as a `p:` blob (`instance_sets.py`); a Collection Instance's objects go as entries of their own under the empty (`collection_copies.py`). Copies and sets of a source object name it in `instanceOf`, so the browser links them. Their ids are derived from their owner's id and their source's name, never written into the file.
- **Coat**: Blender's clearcoat export never carries Coat IOR or Tint and can omit a near-default roughness, so `material_coat.py` reads the coat constants before the export and completes the GLB after it (`extras.coat`, the omitted roughness; wire shape: `../docs/sync-protocol.md`). Coat state and constants are judged on the InlineShaderNodes tree the exporter reads (`material_coat.inlined_principled`), never the raw node tree. Base-channel bakes run inside `shader_bake.coat_disabled()`, which keeps the coat out of them (why: its docstring).
- **Thickness Mode**: glTF has no slot for it, so `material_thickness.py` adds `extras.thicknessMode` (Slab only) to a transmissive material after the export; the thickness itself never travels — the browser sizes glass by its mesh (wire shape: `../docs/sync-protocol.md`).

## Headless

Scripts run with `blender -b` get no timers; `skyray.headless` does the timer's job in a loop (the worker thread still beats), and a Send runs to the end inside the call:

```bash
SKYRAY_SERVER_URL=http://localhost:3600 SKYRAY_TOKEN=skyray_… XDG_CONFIG_HOME=/tmp/skyray-test \
  blender -b scene.blend --python script.py
```

```python
import bpy
from skyray import headless  # the enabled add-on, or the repo's src/ loaded as below

headless.bind_project("<project id>")  # this session's project, as Choose does
status = headless.wait_ready(30)  # READY once a browser has the project open; else the last status
report = headless.send()  # every channel, scope ALL; send(("objects",), "SELECTED", force=True) etc.
print(report.ok, report.message, report.exported, report.unchanged)
```

The per-channel operators (`bpy.ops.skyray.send_scene(scope="all")`, `send_camera`, …) run to the end the same way.

Without installing, a script loads the repo's `src/` under the add-on's module name first:

```python
import importlib.util, sys
spec = importlib.util.spec_from_file_location("skyray", "<repo>/src/__init__.py")
skyray = sys.modules["skyray"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skyray)
skyray.register()
```

`SKYRAY_TOKEN` is used instead of a pairing and never revoked on exit (only Disconnect revokes it); a DEV_TOOLS server issues one with `POST /api/dev/device-token {"user": "claude"}`. Without it, `bpy.ops.skyray.connect()` pairs, and `POST /api/devices/code/<code>/approve` with the account's access token approves. A temp `XDG_CONFIG_HOME` keeps a test's link journals away from the real ones. `headless.wait_until(predicate)` waits for any other status (e.g. a pairing code).

## Doc Map

| File | Load it when… |
|---|---|
| `docs/file-structure.md` | navigating `src/` or deciding where a new file belongs |
| `../docs/sync-protocol.md` | the wire payloads this plugin builds, transform/axis/rotation rules, the editor↔server connection |
| `../skyray-web/test/material/README.md` | changing material export |
| `../docs/tech-decisions.md` | cross-repo architecture facts |
| `../CLAUDE.md` | cross-repo architecture, product vision, shared principles |
