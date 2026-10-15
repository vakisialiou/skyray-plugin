from . import bl_info

# The server when neither SKYRAY_SERVER_URL nor the developer preference names
# another one (skyray-api's default port).
DEFAULT_SERVER_URL = "http://localhost:3500"

# The editor↔server contract version: a server on another version answers
# 426 and the panel asks for an add-on update.
PROTOCOL_VERSION = 4

# Editor family, as the server lists devices and editors.
CLIENT_KIND = "blender"

PLUGIN_VERSION = ".".join(str(part) for part in bl_info["version"])

# Bumped whenever the exported glb changes for the same scene (exporter
# flags, packing, bake logic), so an object key from an older add-on never
# matches a newer export.
EXPORT_VERSION = 6
