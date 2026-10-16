---
name: rebuild-plugin-zip
description: Rebuild the add-on's zips (skyray-local.zip, skyray-dev.zip, skyray-prod.zip) and reinstall after changing anything under src/ in skyray-plugin. Use whenever you edit a .py file in that folder, before telling the user the change is ready to test.
---

# Rebuild the zips

Blender runs the installed zip, not this source tree. A source edit does nothing until the zip is rebuilt and reinstalled.

1. From the `skyray-plugin` repo root, one command builds all three into the root (or into the folder given as its argument):
   ```bash
   python3 build.py
   ```
   Each zip holds the whole `src/` as `skyray/`, the add-on's module name, and has its own server and name:

   | Zip | Add-on name | Default server |
   |---|---|---|
   | `skyray-local.zip` | Skyray (local) | `http://localhost:3500` |
   | `skyray-dev.zip` | Skyray (dev) | `https://dev-gateway.skyray.app` |
   | `skyray-prod.zip` | Skyray | `https://gateway.skyray.app` |

2. Tell the user to reinstall the zip for their server: Blender Preferences → Add-ons → Install from Disk. This machine's api uses `skyray-local.zip`. Any zip replaces the one installed before it.
3. Tell the user to **restart Blender**. Never trust a hot-reinstall: a running session keeps the old modules in `sys.modules`, so the stale copy stays active and its bugs look like regressions. A different build installed over a running one also keeps the old server and panel title until the restart.

Do this immediately after the source change, not batched with other work. A forgotten rebuild is the usual cause of "my fix didn't work".
