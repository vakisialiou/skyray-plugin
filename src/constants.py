from . import bl_info

# The server when neither SKYRAY_SERVER_URL nor the developer preference names
# another one: this build's. build.py writes each zip's own (local, dev,
# prod); the source tree is the local build (skyray-api's default port).
DEFAULT_SERVER_URL = "http://localhost:3500"

# The editor↔server contract version: a server on another version answers
# 426 and the panel asks for an add-on update.
PROTOCOL_VERSION = 4

# Editor family, as the server lists devices and editors.
CLIENT_KIND = "blender"

PLUGIN_VERSION = ".".join(str(part) for part in bl_info["version"])

# bl_info's name, its build's label included ("Skyray (dev)"; "Skyray" for
# prod): the panel's title too, so the sidebar shows which build this is.
ADDON_NAME = bl_info["name"]

# Bumped whenever the exported glb changes for the same scene (exporter
# flags, packing, bake logic), so an object key from an older add-on never
# matches a newer export.
EXPORT_VERSION = 6
