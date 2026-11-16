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
       hide     Type hint: int    a Number Slider (integers, 0 to 8): the group to hide; 0 shows every group.
                                  Set the input to List Access to plug in several sliders (or a list)
                                  and hide several groups
       edges    Type hint: bool   optional: False turns the edge lines off (they're on by default)
       shading  Type hint: bool   optional: False draws the cubes in flat colour, without light
       dots     Type hint: bool   optional: False hides the text dots groups published with their cube
       negative Type hint: bool   optional: True shows, very ghosted, the objects that cut each cube's
                                  voids (published as negative space); off by default
       server   Type hint: str    overrides SERVER_URL below
  4. Set up the outputs:
       M       the meshes, one branch per group ({1}, {2}, ...), already coloured
       C       one colour per branch of M
       G       the group number of each branch of M
       status  what happened
  5. Optional: right-click the component > "Create User Object..." so it's in your toolbar next time.

The component draws every group like the class screen: shaded in its colour with a matte finish
(lit by the viewport's lights, with soft fill on the shadow side), and the same dark edge lines
wherever the surface turns by more than 28 degrees, so the outline of every void reads clearly.
It follows the component's preview: hide the component and the cubes go too. Grasshopper's own
(flat) preview of M is switched off for this. For transparency or other materials, turn the
component's preview off and connect M and C to a Custom Preview (with a Create Material in between).
Bake M to keep a copy in Rhino (the meshes carry their colour). Text dots that a group published
with its cube (the TextDots option of the Publish button) show like Rhino's own dots, as dark tags
(in their own colour with colours = "model").

Only versions that are new since the last check are downloaded. Models arrive at their real
coordinates and are scaled from the published metres to this Rhino file's units, so they line up
with the template. A new publish can take up to a minute to show up (the server's list is cached);
pressing Update on the class screen rebuilds that list straight away.

Works in Rhino 7 and Rhino 8 (Windows and Mac).
"""
import json
import math
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
EDGE_INK = "#1e232a"   # the class screen's edge lines: this ink at 55% over the surface colour
EDGE_OPACITY = 0.55
CREASE_DEG = 28        # an edge is drawn where the faces meet at more than this (EdgesGeometry(g, 28))
EDGE_THICKNESS = 1     # pixels, like the screen's WebGL lines
DOT_INK = "#1e232a"    # text dots: dark tags, like on the class screen
SHADE_GLOW = 0.25      # share of the colour that shows even where no light falls
NEGATIVE_TAG = ("workshoplive", "negative")   # negative space is marked with this user text
GHOST_TRANSPARENCY = 0.88           # very ghosted, like the class screen (12% opaque)
CACHE_VERSION = 3                   # versions loaded by an earlier script are loaded again


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


def text_on(c):
    """Dark or white text, whichever reads on this colour (the class screen's rule)."""
    lin = lambda v: (v / 255.0) / 12.92 if v <= 10 else ((v / 255.0 + 0.055) / 1.055) ** 2.4
    light = 0.2126 * lin(c.R) + 0.7152 * lin(c.G) + 0.0722 * lin(c.B)
    return hex_colour(DOT_INK) if light > 0.18 else System.Drawing.Color.White


def edge_colour(surface):
    """The screen's semi-transparent ink as it looks over this surface colour."""
    ink = hex_colour(EDGE_INK)
    mix = lambda a, b: int(round(a + (b - a) * EDGE_OPACITY))
    return System.Drawing.Color.FromArgb(mix(surface.R, ink.R), mix(surface.G, ink.G), mix(surface.B, ink.B))


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
    """Download one published .3dm. Returns (parts, dots, ghosts): [(mesh in this file's units,
    published colour or None)], [(point in this file's units, text, published colour or None)] and
    the negative space, like the parts."""
    f = read_3dm(http_get(url))
    if f is None:
        raise Exception("not a readable .3dm file")
    units = f.Settings.ModelUnitSystem
    if units == getattr(Rhino.UnitSystem, "None"):   # .None is a syntax error in Python 3
        units = Rhino.UnitSystem.Meters   # what the Publish button and WorkshopPush.py write
    doc = Rhino.RhinoDoc.ActiveDoc
    scale = Rhino.RhinoMath.UnitScale(units, doc.ModelUnitSystem) if doc is not None else 1.0
    xform = rg.Transform.Scale(rg.Point3d.Origin, scale)
    parts, dots, ghosts = [], [], []
    for obj in f.Objects:
        negative = is_negative(f, obj.Attributes)
        if isinstance(obj.Geometry, rg.TextDot):
            if obj.Geometry.Text and not negative:
                dots.append((xform * obj.Geometry.Point, obj.Geometry.Text, object_colour(f, obj.Attributes)))
            continue
        mesh = as_mesh(obj.Geometry)
        if mesh is None or mesh.Faces.Count == 0:
            continue
        if abs(scale - 1.0) > 1e-12:
            mesh.Transform(xform)
        mesh.Normals.ComputeNormals()
        (ghosts if negative else parts).append((mesh, object_colour(f, obj.Attributes)))
    return parts, dots, ghosts


def is_negative(f, attrs):
    """Whether an object was published as negative space (never part of the cube). Only the user
    text counts, never the layer: files from the first version of the option put the cube on the
    "Negative space" layer too (it was the file's first layer)."""
    try:
        return attrs.GetUserString(NEGATIVE_TAG[0]) == NEGATIVE_TAG[1]
    except Exception:
        return False


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
        if old is not None and old["url"] == url and old.get("v") == CACHE_VERSION:
            blocks[key] = old
            continue
        try:
            parts, dots, ghosts = load_model(url)
        except Exception as ex:
            problems.append("Group %s: download failed (%s)" % (key, ex))
            if old is not None:
                blocks[key] = old       # keep showing the version we had
            continue
        blocks[key] = {"url": url, "parts": parts, "dots": dots, "ghosts": ghosts, "v": CACHE_VERSION,
                       "publishedAt": entry.get("publishedAt", ""), "size": entry.get("size") or 0}
    memory["blocks"] = blocks
    return problems


def crease_lines(mesh):
    """The mesh's outline as the class screen draws it: open edges, and edges where the two faces
    meet at more than CREASE_DEG. Triangles on a flat or gently curved face are left out."""
    limit = math.cos(math.radians(CREASE_DEG))
    if mesh.FaceNormals.Count != mesh.Faces.Count:
        mesh.FaceNormals.ComputeFaceNormals()
    normals = mesh.FaceNormals
    topology = mesh.TopologyEdges      # joins faces that share a corner position, welded or not
    lines = []
    for i in range(topology.Count):
        faces = topology.GetConnectedFaces(i)
        if faces.Length == 2:
            a, b = normals[faces[0]], normals[faces[1]]
            dot = a.X * b.X + a.Y * b.Y + a.Z * b.Z
            if dot >= limit or (a.X == a.Y == a.Z == 0) or (b.X == b.Y == b.Z == 0):
                continue      # smooth, or a degenerate sliver face without a direction
        lines.append(topology.EdgeLine(i))
    return System.Array[rg.Line](lines)


def previewing(component):
    """Whether Grasshopper is showing this component's preview right now."""
    doc = component.OnPingDocument()
    if doc is None or component.Hidden or component.Locked:
        return False          # deleted, preview off, or disabled
    canvas = Grasshopper.Instances.ActiveCanvas
    if canvas is None or canvas.Document is None or not canvas.Document.Equals(doc):
        return False          # another Grasshopper file is open
    try:
        if doc.PreviewMode == Grasshopper.Kernel.GH_PreviewMode.Disabled:
            return False
        if doc.PreviewFilter == Grasshopper.Kernel.GH_PreviewFilter.Selected and not component.Attributes.Selected:
            return False
    except Exception:
        pass
    return True


_materials = {}


def shaded_material(colour):
    """A matte material like the class screen's (rough, no shine), lit by the viewport's lights.
    A little of the colour glows by itself, like the screen's soft sky light, so the sides turned
    away from the light stay readable."""
    key = colour.ToArgb()
    if key not in _materials:
        m = Rhino.Display.DisplayMaterial(colour)
        glow = lambda v: int(round(v * SHADE_GLOW))
        for side in ("", "Back"):
            try:
                setattr(m, side + "Diffuse", colour)
                setattr(m, side + "Specular", System.Drawing.Color.Black)
                setattr(m, side + "Shine", 0.0)
                setattr(m, side + "Emission", System.Drawing.Color.FromArgb(glow(colour.R), glow(colour.G), glow(colour.B)))
            except Exception:
                pass
        try:
            m.IsTwoSided = True     # the screen draws both sides, so voids look the same from inside
        except Exception:
            pass
        _materials[key] = m
    return _materials[key]


def ghost_material(colour):
    """Negative space: the colour, very see-through, no shine."""
    key = ("ghost", colour.ToArgb())
    if key not in _materials:
        m = Rhino.Display.DisplayMaterial(colour, GHOST_TRANSPARENCY)
        try:
            m.IsTwoSided = True
            for side in ("", "Back"):
                setattr(m, side + "Specular", System.Drawing.Color.Black)
                setattr(m, side + "Shine", 0.0)
                setattr(m, side + "Transparency", GHOST_TRANSPARENCY)
        except Exception:
            pass
        _materials[key] = m
    return _materials[key]


def pale(colour, t=0.5):
    """The colour, `t` of the way to white."""
    mix = lambda v: int(round(v + (255 - v) * t))
    return System.Drawing.Color.FromArgb(mix(colour.R), mix(colour.G), mix(colour.B))


def hide_grasshopper_preview(name):
    """Stop Grasshopper drawing an output itself (it draws coloured meshes flat, without light), so
    this component's shaded drawing is the only one. Returns False if that can't be done."""
    for param in ghenv.Component.Params.Output:
        if param.NickName == name:
            try:
                if not param.Hidden:
                    param.Hidden = True
                return True
            except Exception:
                return False
    return True     # no such output: nothing for Grasshopper to draw


def install_drawing(memory):
    """Draw memory["draw"] in the Rhino viewports: each cube shaded, then its edge lines, after
    Rhino's own objects so the depth buffer hides what's behind. Re-installed on every run, so the
    newest version of the script is the one drawing."""
    key = "workshoplive_pull_drawing_" + str(ghenv.Component.InstanceGuid)
    old = sc.sticky.get(key)
    if old is not None:
        try:
            Rhino.Display.DisplayPipeline.PostDrawObjects -= old[0]
            Rhino.Display.DisplayPipeline.CalculateBoundingBox -= old[1]
        except Exception:
            pass
    old = sc.sticky.pop("workshoplive_pull_edges_" + str(ghenv.Component.InstanceGuid), None)
    if old is not None:     # the edges-only drawing of an earlier version of this script
        try:
            Rhino.Display.DisplayPipeline.PostDrawObjects -= old
        except Exception:
            pass
    component = ghenv.Component

    def draw(sender, e):
        try:
            if not (memory["draw"] or memory.get("dots") or memory.get("ghosts")) or not previewing(component):
                return
            for mesh, material, lines, ink in memory["draw"]:   # all surfaces first, then all lines
                if mesh is not None:
                    e.Display.DrawMeshShaded(mesh, material)
            ghosts = memory.get("ghosts") or []
            for mesh, material, lines, ink in memory["draw"] + ghosts:
                if lines is None:
                    continue
                try:
                    e.Display.DrawLines(lines, ink, EDGE_THICKNESS)
                except Exception:
                    for line in lines:
                        e.Display.DrawLine(line, ink, EDGE_THICKNESS)
            for mesh, material, lines, ink in ghosts:   # see-through, so after everything solid
                e.Display.DrawMeshShaded(mesh, material)
            for point, text, back, fore in memory.get("dots") or []:   # always on top, like Rhino's dots
                e.Display.DrawDot(point, text, back, fore)
        except Exception:
            pass    # never let a drawing problem interrupt Rhino's redraw

    def bounds(sender, e):
        try:   # so Rhino's near and far clipping planes leave room for the cubes
            if memory.get("bbox") is not None and (memory["draw"] or memory.get("dots") or memory.get("ghosts")) and previewing(component):
                e.IncludeBoundingBox(memory["bbox"])
        except Exception:
            pass

    Rhino.Display.DisplayPipeline.PostDrawObjects += draw
    Rhino.Display.DisplayPipeline.CalculateBoundingBox += bounds
    sc.sticky[key] = (draw, bounds)


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
memory.setdefault("draw", [])
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
edges_on = bool(inp("edges", True))
dots_on = bool(inp("dots", True))
negative_on = bool(inp("negative", False))
shading_on = bool(inp("shading", True)) and hide_grasshopper_preview("M")
if not shading_on:
    for param in ghenv.Component.Params.Output:
        if param.NickName == "M":
            try:
                param.Hidden = False     # flat colour: let Grasshopper draw M again
            except Exception:
                pass


def hidden_groups(value):
    """The group numbers to leave out: one number or a list of them; 0 (or nothing) hides none."""
    if value is None:
        return set()
    if isinstance(value, (str, int, float)) or not hasattr(value, "__iter__"):
        value = [value]
    out = set()
    for item in value:
        try:
            n = int(round(float(item)))
        except (TypeError, ValueError):
            n = group_number(item)
        if n:
            out.add(n)
    return out


hide = hidden_groups(inp("hide"))

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
draw = []
dots = []
ghost_draw = []
bbox = None     # of everything drawn (a .NET struct, so it's replaced, never changed in place)
keys = sorted(memory["blocks"].keys(), key=sort_key)
hidden_here = [key for key in keys if group_number(key) in hide]
for i, key in enumerate(keys):
    block = memory["blocks"][key]
    n = group_number(key)
    if n in hide:
        lines.append("Group %s: hidden" % key)
        continue
    path = GH_Path(n if n is not None else 1000 + i)
    fallback = group_colour(key) if mode != "white" else hex_colour(WHITE)
    branch_colour = None
    if edges_on and "edges" not in block:
        block["edges"] = [crease_lines(mesh) for mesh, published in block["parts"]]   # once per version
    for j, (mesh, published) in enumerate(block["parts"]):
        colour = fallback
        if mode == "model" and published is not None and not is_black(published):
            colour = published
        if branch_colour is None:
            branch_colour = colour
        shown = mesh.DuplicateMesh()
        shown.VertexColors.CreateMonotoneMesh(colour)   # keeps the colour when M is baked or previewed flat
        M.Add(shown, path)
        edge_lines = block["edges"][j] if edges_on and block["edges"][j].Length else None
        if shading_on:
            draw.append((mesh, shaded_material(colour), edge_lines, edge_colour(colour)))
            box = mesh.GetBoundingBox(False)
            bbox = box if bbox is None else rg.BoundingBox.Union(bbox, box)
        elif edge_lines is not None:
            draw.append((None, None, edge_lines, edge_colour(colour)))
    if negative_on and block.get("ghosts"):
        if "ghost_edges" not in block:
            block["ghost_edges"] = [crease_lines(mesh) for mesh, published in block["ghosts"]]
        for (mesh, published), edge_lines in zip(block["ghosts"], block["ghost_edges"]):
            colour = fallback
            if mode == "model" and published is not None and not is_black(published):
                colour = published
            tint = pale(colour)
            ghost_draw.append((mesh, ghost_material(tint), edge_lines if edge_lines.Length else None,
                               pale(edge_colour(tint), 0.6)))
            box = mesh.GetBoundingBox(False)
            bbox = box if bbox is None else rg.BoundingBox.Union(bbox, box)
    block_dots = block.get("dots") or []      # versions loaded by an earlier script have none
    if dots_on:
        for point, text, published in block_dots:
            back = published if mode == "model" and published is not None and not is_black(published) else hex_colour(DOT_INK)
            dots.append((point, text, back, text_on(back)))
            box = rg.BoundingBox(point, point)
            bbox = box if bbox is None else rg.BoundingBox.Union(bbox, box)
    C.append(branch_colour or fallback)
    G.append(n if n is not None else key)
    lines.append("Group %s: published %s (%.2f MB%s)" % (
        key, local_time(block["publishedAt"]), block["size"] / 1048576.0,
        ", %d text dot%s" % (len(block_dots), "" if len(block_dots) == 1 else "s") if block_dots else ""))

memory["draw"] = draw
memory["dots"] = dots
memory["ghosts"] = ghost_draw
memory["bbox"] = bbox
install_drawing(memory)

ghenv.Component.Message = "%d group%s%s" % (len(keys), "" if len(keys) == 1 else "s",
                                            ", %d hidden" % len(hidden_here) if hidden_here else "")
if memory["checked"] is None:
    head = "Not checked yet."
elif keys:
    head = "%d group%s on the server, checked at %s%s." % (
        len(keys), "" if len(keys) == 1 else "s", memory["checked"],
        ", checking every %d s" % every if auto_on else "")
else:
    head = "No groups published yet (checked at %s)." % memory["checked"]
status = "\n".join([head] + lines + memory["problems"])
