# -*- coding: utf-8 -*-
"""
Workshop Live: pull every group's latest published cube into Grasshopper, coloured by group.

SET-UP (once)
  1. Drop a Python script component on the canvas
     (Rhino 7: Maths > Script > GhPython Script; Rhino 8: Maths > Script > Python 3 Script or IronPython 2 Script).
  2. Open it and paste this whole file in, replacing what's there.
  3. Set up the inputs (zoom in on the component, use + / - and right-click a name to rename it).
     All of them are optional:
       refresh  Type hint: bool   connect a Button: each click checks the server for new versions
       auto     Type hint: bool   a Boolean Toggle: checks the server by itself every `every` seconds
       every    Type hint: int    seconds between automatic checks (default 30, at least 10)
       colours  Type hint: str    "group" (default, the class screen's colours), "model" (the colours
                                  the groups published with) or "white"
       server   Type hint: str    overrides SERVER_URL below
  4. Set up the outputs:
       M       the meshes, one branch per group ({1}, {2}, ...), already coloured
       C       one colour per branch of M
       G       the group number of each branch of M
       status  what happened
  5. Optional: right-click the component > "Create User Object..." so it's in your toolbar next time.

The component's own preview shows every group in its colour (the colour is stored on the meshes),
so nothing else needs connecting. For transparency or other materials, turn the component's preview
off and connect M and C to a Custom Preview (Display > Preview > Custom Preview, with a Create
Material in between). Bake M to keep a copy in Rhino.

Only versions that are new since the last check are downloaded. Models arrive at their real
coordinates and are scaled from the published metres to this Rhino file's units, so they line up
with the template. A new publish can take up to a minute to show up (the server's list is cached);
pressing Update on the class screen rebuilds that list straight away.

Works in Rhino 7 and Rhino 8 (Windows and Mac).
"""
import json
import time
import System
import Rhino
import Rhino.Geometry as rg
import scriptcontext as sc
import Grasshopper
from Grasshopper import DataTree
from Grasshopper.Kernel.Data import GH_Path

# ---------------------------------------------------------------------------
# Instructor: same line as in WorkshopPublish.py.
SERVER_URL = "https://tainanrocks.vercel.app"
# ---------------------------------------------------------------------------

DEFAULT_EVERY_S = 30
MIN_EVERY_S = 10      # the server's list only changes about once every 6 s anyway

# The class screen's colours (public/index.html): vermilion, marigold, leaf, jade, blue, violet, rose, earth.
GROUP_COLOURS = ["#e05a3a", "#f2b134", "#7fb04b", "#2a9d8f", "#3a78c2", "#6a4c9c", "#d9779f", "#8d6e4e"]
EXTRA_COLOURS = ["#ff48b0", "#0078bf", "#ff6c2f", "#765ba7", "#00838a", "#ffb511", "#62a8e5", "#bb8b41"]
WHITE = "#f6f7f5"


def inp(name, default=None):
    """An input's value, or `default` if the input doesn't exist or isn't connected."""
    value = globals().get(name, None)
    return default if value is None else value


def warn(text, error=False):
    level = Grasshopper.Kernel.GH_RuntimeMessageLevel.Error if error else Grasshopper.Kernel.GH_RuntimeMessageLevel.Warning
    ghenv.Component.AddRuntimeMessage(level, text)


# ---------------------------------------------------------------- colours
def hex_colour(value):
    value = value.lstrip("#")
    return System.Drawing.Color.FromArgb(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def group_number(key):
    """Published IDs "3", "G3", "GROUP3", "Group-3" all mean group 3 (same rule as the class screen)."""
    text = str(key).upper()
    for prefix in ("GROUP", "GRP", "G"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text[:1] in ("-", "_"):
        text = text[1:]
    return int(text) if text.isdigit() and len(text) <= 3 else None


def group_colour(key):
    n = group_number(key)
    if n and n <= len(GROUP_COLOURS):
        return hex_colour(GROUP_COLOURS[n - 1])
    if n:
        return hex_colour(EXTRA_COLOURS[(n - 1) % len(EXTRA_COLOURS)])
    h = 0
    for ch in str(key):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return hex_colour(EXTRA_COLOURS[h % len(EXTRA_COLOURS)])


def is_black(c):
    return c.R == 0 and c.G == 0 and c.B == 0


# ---------------------------------------------------------------- network
def web_client():
    try:
        System.Net.ServicePointManager.SecurityProtocol = (
            System.Net.ServicePointManager.SecurityProtocol | System.Net.SecurityProtocolType.Tls12)
    except Exception:
        pass
    return System.Net.WebClient()


def http_get(url):
    """The response body as .NET bytes."""
    try:
        return web_client().DownloadData(url)
    except System.Net.WebException as ex:
        if ex.Response is None:
            raise Exception("can't reach %s (%s)" % (url, ex.Message))
        raise Exception("%s answered %s" % (url, System.Convert.ToInt32(ex.Response.StatusCode)))


def read_manifest(server):
    raw = http_get("%s/api/state" % server.rstrip("/"))
    manifest = json.loads(System.Text.Encoding.UTF8.GetString(raw))
    if manifest.get("error"):
        raise Exception("the server says: %s" % manifest["error"])
    return manifest.get("blocks") or {}


# ---------------------------------------------------------------- models
def read_3dm(data):
    try:
        f = Rhino.FileIO.File3dm.FromByteArray(data)
        if f is not None:
            return f
    except Exception:
        pass
    path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "workshoplive_pull_%s.3dm" % System.Guid.NewGuid())
    System.IO.File.WriteAllBytes(path, data)
    try:
        return Rhino.FileIO.File3dm.Read(path)
    finally:
        try:
            System.IO.File.Delete(path)
        except Exception:
            pass


def object_colour(f, attrs):
    """The colour an object was published with, or None."""
    try:
        if attrs.ColorSource == Rhino.DocObjects.ObjectColorSource.ColorFromObject:
            return attrs.ObjectColor
        layer = f.AllLayers.FindIndex(attrs.LayerIndex)
        return layer.Color if layer is not None else None
    except Exception:
        return None


def as_mesh(geom):
    """Published files hold meshes; mesh anything else surface-like just in case."""
    if isinstance(geom, rg.Mesh):
        return geom.DuplicateMesh()
    if isinstance(geom, rg.Extrusion):
        geom = geom.ToBrep(True)
    if isinstance(geom, rg.Brep):
        parts = rg.Mesh.CreateFromBrep(geom, rg.MeshingParameters.Default)
        if parts:
            joined = rg.Mesh()
            for m in parts:
                joined.Append(m)
            return joined
    return None


def load_model(url):
    """Download one published .3dm. Returns [(mesh in this file's units, published colour or None)]."""
    f = read_3dm(http_get(url))
    if f is None:
        raise Exception("not a readable .3dm file")
    units = f.Settings.ModelUnitSystem
    if units == getattr(Rhino.UnitSystem, "None"):   # .None is a syntax error in Python 3
        units = Rhino.UnitSystem.Meters   # what the Publish button and WorkshopPush.py write
    doc = Rhino.RhinoDoc.ActiveDoc
    scale = Rhino.RhinoMath.UnitScale(units, doc.ModelUnitSystem) if doc is not None else 1.0
    xform = rg.Transform.Scale(rg.Point3d.Origin, scale)
    parts = []
    for obj in f.Objects:
        mesh = as_mesh(obj.Geometry)
        if mesh is None or mesh.Faces.Count == 0:
            continue
        if abs(scale - 1.0) > 1e-12:
            mesh.Transform(xform)
        mesh.Normals.ComputeNormals()
        parts.append((mesh, object_colour(f, obj.Attributes)))
    return parts


def local_time(iso):
    try:
        return System.DateTimeOffset.Parse(iso).LocalDateTime.ToString("HH:mm:ss")
    except Exception:
        return "?"


def sort_key(key):
    n = group_number(key)
    return (0, n, "") if n is not None else (1, 0, str(key))


def check_server(memory, server):
    """Read the server's list and download versions that are new. Returns a list of problems."""
    problems = []
    entries = read_manifest(server)
    blocks = {}
    for key, entry in entries.items():
        url = entry.get("url") if isinstance(entry, dict) else None
        if not url or entry.get("hidden"):
            continue    # hidden from the class screen, so hidden here too
        old = memory["blocks"].get(key)
        if old is not None and old["url"] == url:
            blocks[key] = old
            continue
        try:
            parts = load_model(url)
        except Exception as ex:
            problems.append("Group %s: download failed (%s)" % (key, ex))
            if old is not None:
                blocks[key] = old       # keep showing the version we had
            continue
        blocks[key] = {"url": url, "parts": parts, "publishedAt": entry.get("publishedAt", ""),
                       "size": entry.get("size") or 0}
    memory["blocks"] = blocks
    return problems


def run_again_in(seconds):
    """Ask Grasshopper to run this component again later (for auto)."""
    component = ghenv.Component
    document = component.OnPingDocument()

    def again(doc):
        component.ExpireSolution(False)

    try:
        document.ScheduleSolution(int(seconds * 1000) + 200, Grasshopper.Kernel.GH_Document.GH_ScheduleDelegate(again))
    except Exception:
        pass   # then press refresh


# ---------------------------------------------------------------- the component
memory = sc.sticky.setdefault("workshoplive_pull_" + str(ghenv.Component.InstanceGuid),
                              {"time": 0.0, "blocks": {}, "problems": [], "checked": None})
server_url = str(inp("server", SERVER_URL) or SERVER_URL)
mode = str(inp("colours", "group") or "group").strip().lower()
if mode not in ("group", "model", "white"):
    warn('colours should be "group", "model" or "white"; using "group".')
    mode = "group"
try:
    every = max(MIN_EVERY_S, int(inp("every", DEFAULT_EVERY_S) or DEFAULT_EVERY_S))
except (TypeError, ValueError):
    every = DEFAULT_EVERY_S
auto_on = bool(inp("auto", False))

if "your-project" in server_url.lower():
    warn("Set SERVER_URL at the top of the script (or connect the server input).", error=True)
else:
    due = auto_on and time.time() - memory["time"] >= every - 0.5
    if bool(inp("refresh", False)) or memory["checked"] is None or due:
        memory["time"] = time.time()
        try:
            memory["problems"] = check_server(memory, server_url)
            memory["checked"] = time.strftime("%H:%M:%S")
        except Exception as ex:
            memory["problems"] = ["Couldn't check the server: %s" % ex]
            if memory["checked"] is None:
                memory["checked"] = "never"
    if auto_on:
        run_again_in(max(0.0, every - (time.time() - memory["time"])))

for problem in memory["problems"]:
    warn(problem)

M = DataTree[object]()
C, G = [], []
lines = []
keys = sorted(memory["blocks"].keys(), key=sort_key)
for i, key in enumerate(keys):
    block = memory["blocks"][key]
    n = group_number(key)
    path = GH_Path(n if n is not None else 1000 + i)
    fallback = group_colour(key) if mode != "white" else hex_colour(WHITE)
    branch_colour = None
    for mesh, published in block["parts"]:
        colour = fallback
        if mode == "model" and published is not None and not is_black(published):
            colour = published
        if branch_colour is None:
            branch_colour = colour
        shown = mesh.DuplicateMesh()
        shown.VertexColors.CreateMonotoneMesh(colour)   # this is what colours the component's preview
        M.Add(shown, path)
    C.append(branch_colour or fallback)
    G.append(n if n is not None else key)
    lines.append("Group %s: published %s (%.2f MB)" % (key, local_time(block["publishedAt"]),
                                                       block["size"] / 1048576.0))

ghenv.Component.Message = "%d group%s" % (len(keys), "" if len(keys) == 1 else "s")
if memory["checked"] is None:
    head = "Not checked yet."
elif keys:
    head = "%d group%s on the server, checked at %s%s." % (
        len(keys), "" if len(keys) == 1 else "s", memory["checked"],
        ", checking every %d s" % every if auto_on else "")
else:
    head = "No groups published yet (checked at %s)." % memory["checked"]
status = "\n".join([head] + lines + memory["problems"])
