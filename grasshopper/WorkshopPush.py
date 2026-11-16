# -*- coding: utf-8 -*-
"""
Workshop Live: push geometry from Grasshopper straight to the class screen.

SET-UP (once)
  1. Drop a Python script component on the canvas
     (Rhino 7: Maths > Script > GhPython Script; Rhino 8: Maths > Script > Python 3 Script or IronPython 2 Script).
  2. Open it and paste this whole file in, replacing what's there.
  3. Set up the inputs (zoom in on the component, use + / - and right-click a name to rename it):
       G       Item access: List Access   Type hint: GeometryBase   your cube: Breps, meshes, SubDs, extrusions
       group   Type hint: int             your group number, 1-8 (a Number Slider or Panel)
       push    Type hint: bool            connect a Button: each click publishes once
       auto    Type hint: bool            optional: a Boolean Toggle; publishes whenever G changes,
                                          at most once every 15 seconds
       colour  Type hint: Color           optional: colour of your cube on the screen
       dots    List Access, Type hint: Guid     optional: text dots to publish with your cube, as points
                                          referenced from Rhino (a Point parameter > Set Multiple Points);
                                          each point's Name (Rhino: Properties > Name) is the dot's text
       server  Type hint: str             optional: overrides SERVER_URL below
       key     Type hint: str             optional: overrides WORKSHOP_KEY below
  4. Rename the output to: status
  5. Optional: right-click the component > "Create User Object..." so it's in your toolbar next time.

Everything in G is published together as your group's cube (as meshes, converted to metres),
replacing what your group published before, just like the Publish button in Rhino.

Text dots go along too, like the Publish button's TextDots option: every point in `dots` becomes a
dot that shows its name, at the point (text dots in G are published as dots as well). Points work
where text dots can't be referenced into Grasshopper without plug-ins (Rhino 7). The component
shows each point's name as a dot in Rhino, and points without a name as an orange "no name" dot
(those are left out). Renaming a point in Rhino updates the dot at once, and with auto on
publishes it again.

Works in Rhino 7 and Rhino 8 (Windows and Mac).
"""
import os
import json
import time
import System
import Rhino
import Rhino.Geometry as rg
import scriptcontext as sc
import Grasshopper

# ---------------------------------------------------------------------------
# Instructor: same two lines as in WorkshopPublish.py.
SERVER_URL = "https://tainanrocks.vercel.app"
WORKSHOP_KEY = "blocks-2026"
# ---------------------------------------------------------------------------

GROUPS = 8
DOT_INK = System.Drawing.Color.FromArgb(0x1e, 0x23, 0x2a)          # dark tags, like on the class screen
NO_NAME = System.Drawing.Color.FromArgb(0xe0, 0x8a, 0x1c)          # a point that still needs a name
MAX_BYTES = 4 * 1024 * 1024
AUTO_EVERY_S = 15     # auto: at most one publish per this many seconds


def inp(name, default=None):
    """An input's value, or `default` if the input doesn't exist or isn't connected."""
    value = globals().get(name, None)
    return default if value is None else value


def warn(text, error=False):
    level = Grasshopper.Kernel.GH_RuntimeMessageLevel.Error if error else Grasshopper.Kernel.GH_RuntimeMessageLevel.Warning
    ghenv.Component.AddRuntimeMessage(level, text)


# ---------------------------------------------------------------- geometry
def as_geometry(item):
    """Grasshopper hands over geometry, wrapped geometry or Rhino object ids; return RhinoCommon geometry."""
    if item is None:
        return None
    if isinstance(item, System.Guid):
        obj = Rhino.RhinoDoc.ActiveDoc.Objects.FindId(item)
        return obj.Geometry if obj else None
    if isinstance(item, rg.Box):
        return item.ToBrep()
    if hasattr(item, "Value") and not isinstance(item, rg.GeometryBase):
        return as_geometry(item.Value)
    return item


def to_meshes(geom, mp):
    """Meshes for any surface-like geometry, else []."""
    if isinstance(geom, rg.Mesh):
        return [geom.DuplicateMesh()]
    brep = None
    if isinstance(geom, rg.Brep):
        brep = geom
    elif isinstance(geom, rg.Extrusion):
        brep = geom.ToBrep(True)
    elif isinstance(geom, rg.Surface):
        brep = rg.Brep.CreateFromSurface(geom)
    elif isinstance(geom, rg.SubD):
        try:
            m = rg.Mesh.CreateFromSubD(geom, 3)
        except Exception:
            m = rg.Mesh.CreateFromSubDControlNet(geom)
        return [m] if m else []
    if brep is None:
        return []
    meshes = rg.Mesh.CreateFromBrep(brep, mp)
    return [m for m in meshes] if meshes else []


def to_metres():
    doc = Rhino.RhinoDoc.ActiveDoc
    return rg.Transform.Scale(rg.Point3d.Origin, Rhino.RhinoMath.UnitScale(doc.ModelUnitSystem, Rhino.UnitSystem.Meters))


def make_dot(text, point, scale, like=None):
    """A text dot in metres (keeping the font of `like`, a dot it's copied from)."""
    dot = rg.TextDot(text, scale * point)
    if like is not None:
        try:
            dot.FontHeight = like.FontHeight
            dot.FontFace = like.FontFace
        except Exception:
            pass
    return dot


def referenced_id(item):
    """The Rhino object id behind an input item: a Guid (type hint Guid), or a referenced
    Grasshopper point (no type hint). None for anything else, e.g. a plain Point3d."""
    if isinstance(item, System.Guid):
        return item if item != System.Guid.Empty else None
    try:
        if item.IsReferencedGeometry and item.ReferenceID != System.Guid.Empty:
            return item.ReferenceID
    except Exception:
        pass
    return None


def named_points(items):
    """The Rhino points in `dots`: [(id, location in this file's units, name)], plus how many items
    aren't referenced Rhino points. The name is the point's Name in Rhino (Properties), or ""."""
    doc = Rhino.RhinoDoc.ActiveDoc
    if items is not None and (isinstance(items, (System.Guid, str)) or not hasattr(items, "__iter__")):
        items = [items]       # the input is still on Item Access: one point
    points, bad = [], 0
    for item in items or []:
        ref = referenced_id(item)
        obj = doc.Objects.FindId(ref) if ref is not None else None
        if obj is None or obj.IsDeleted or not isinstance(obj.Geometry, rg.Point):
            bad += 1
            continue
        points.append((obj.Id, obj.Geometry.Location, (obj.Attributes.Name or "").strip()))
    return points, bad


# ---------------------------------------------------------------- the dots in the Rhino viewport
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


def install_dot_preview(memory):
    """Show every point in `dots` as a text dot with its name in the Rhino viewports (a point
    without a name gets an orange "no name" dot), and re-run the component when one of those points
    is renamed, so the preview, and auto, follow the new name straight away. Re-installed on every
    run, so the newest version of the script is the one drawing."""
    component = ghenv.Component
    key = "workshoplive_gh_dots_" + str(component.InstanceGuid)
    old = sc.sticky.get(key)
    if old is not None:
        try:
            Rhino.Display.DisplayPipeline.PostDrawObjects -= old[0]
            Rhino.RhinoDoc.ModifyObjectAttributes -= old[1]
        except Exception:
            pass

    def draw(sender, e):
        try:
            if memory["preview"] and previewing(component):
                for point, text, back, fore in memory["preview"]:
                    e.Display.DrawDot(point, text, back, fore)
        except Exception:
            pass    # never let a drawing problem interrupt Rhino's redraw

    def renamed(sender, e):
        try:
            if str(e.RhinoObject.Id) in memory["dot_ids"] and component.OnPingDocument() is not None:
                run_again_in(0)
        except Exception:
            pass

    Rhino.Display.DisplayPipeline.PostDrawObjects += draw
    Rhino.RhinoDoc.ModifyObjectAttributes += renamed
    sc.sticky[key] = (draw, renamed)


def collect(items):
    """All input geometry as meshes in metres, and the text dots among it. Returns (meshes, dots, skipped count)."""
    doc = Rhino.RhinoDoc.ActiveDoc
    try:
        mp = doc.GetMeshingParameters(doc.MeshingParameterStyle)
    except Exception:
        mp = rg.MeshingParameters.Default
    scale = to_metres()
    meshes, dots, skipped = [], [], 0
    for item in items or []:
        geom = as_geometry(item)
        if isinstance(geom, rg.TextDot):
            if geom.Text:
                dots.append(make_dot(geom.Text, geom.Point, scale, like=geom))
            continue
        parts = [m for m in to_meshes(geom, mp) if m is not None and m.Faces.Count > 0]
        if not parts:
            skipped += 1
            continue
        for m in parts:
            m.Transform(scale)
            m.Normals.ComputeNormals()
            m.Compact()
            meshes.append(m)
    return meshes, dots, skipped


def fingerprint(meshes, dots, extra):
    """Cheap signature of the geometry, so auto only publishes when something really changed."""
    out = [extra]
    for d in dots:
        out.append("dot %s@%.5f,%.5f,%.5f" % (d.Text, d.Point.X, d.Point.Y, d.Point.Z))
    for m in meshes:
        b = m.GetBoundingBox(False)
        out.append("%d/%d/%.5f,%.5f,%.5f,%.5f,%.5f,%.5f" % (m.Vertices.Count, m.Faces.Count,
                                                          b.Min.X, b.Min.Y, b.Min.Z, b.Max.X, b.Max.Y, b.Max.Z))
    return "|".join(out)


def write_3dm(meshes, dots, colour, group):
    f = Rhino.FileIO.File3dm()
    f.Settings.ModelUnitSystem = Rhino.UnitSystem.Meters
    for m in meshes:
        attr = Rhino.DocObjects.ObjectAttributes()
        if colour is not None:
            attr.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
            attr.ObjectColor = colour
        f.Objects.AddMesh(m, attr)
    for d in dots:     # no colour of their own: the screen draws them as dark tags
        try:
            f.Objects.AddTextDot(d, Rhino.DocObjects.ObjectAttributes())
        except Exception:
            f.Objects.AddTextDot(d.Text, d.Point)
    path = os.path.join(System.IO.Path.GetTempPath(), "workshoplive_gh_group%d.3dm" % group)
    if not f.Write(path, 7):
        raise Exception("could not write the temporary file %s" % path)
    return path


# ---------------------------------------------------------------- network
def http_post(url, data, key):
    try:
        System.Net.ServicePointManager.SecurityProtocol = (
            System.Net.ServicePointManager.SecurityProtocol | System.Net.SecurityProtocolType.Tls12)
    except Exception:
        pass
    wc = System.Net.WebClient()
    wc.Headers.Add("x-workshop-key", key)
    wc.Headers.Add("Content-Type", "application/octet-stream")
    try:
        raw = wc.UploadData(url, "POST", data)
        return 200, System.Text.Encoding.UTF8.GetString(raw)
    except System.Net.WebException as ex:
        if ex.Response is None:
            raise Exception("can't reach %s (%s)" % (url, ex.Message))
        status = System.Convert.ToInt32(ex.Response.StatusCode)
        return status, System.IO.StreamReader(ex.Response.GetResponseStream()).ReadToEnd()


def publish(meshes, dots, group, colour, server, key):
    """Returns (ok, message)."""
    if "your-project" in server.lower():
        return False, "Set SERVER_URL at the top of the script (or connect the server input)."
    path = write_3dm(meshes, dots, colour, group)
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        return False, "Group %d's geometry is %.1f MB; the limit is 4 MB. Simplify it and push again." % (group, size / 1048576.0)
    status, body = http_post("%s/api/publish?block=%d" % (server.rstrip("/"), group), System.IO.File.ReadAllBytes(path), key)
    try:
        result = json.loads(body)
    except Exception:
        result = {"error": body[:200]}
    if status == 200 and result.get("ok"):
        extra = ", %d text dot%s" % (len(dots), "" if len(dots) == 1 else "s") if dots else ""
        return True, "Published Group %d at %s (%.2f MB%s)." % (group, time.strftime("%H:%M:%S"), size / 1048576.0, extra)
    return False, "Publish failed (%s): %s" % (status, result.get("error", "unknown error"))


def run_again_in(seconds):
    """Ask Grasshopper to run this component again later (for auto's waiting time)."""
    component = ghenv.Component
    document = component.OnPingDocument()

    def again(doc):
        component.ExpireSolution(False)

    try:
        document.ScheduleSolution(int(seconds * 1000) + 200, Grasshopper.Kernel.GH_Document.GH_ScheduleDelegate(again))
    except Exception:
        pass   # then the next change (or the Push button) publishes it


# ---------------------------------------------------------------- the component
memory = sc.sticky.setdefault("workshoplive_gh_" + str(ghenv.Component.InstanceGuid),
                              {"time": 0.0, "fingerprint": None, "status": "Ready: connect geometry and press Push."})
group_number = int(inp("group", 0) or 0)
colour_value = inp("colour")
server_url = str(inp("server", SERVER_URL) or SERVER_URL)
workshop_key = str(inp("key", WORKSHOP_KEY) or WORKSHOP_KEY)
ghenv.Component.Message = "Group %d" % group_number if 1 <= group_number <= GROUPS else "no group"

meshes, g_dots, skipped = collect(inp("G", []))
if skipped:
    warn("%d item(s) in G aren't surfaces, polysurfaces, meshes, SubDs or text dots and were left out." % skipped)
memory.setdefault("preview", [])
memory.setdefault("dot_ids", set())
points, not_points = named_points(inp("dots", []))
if not_points:
    warn("%d item(s) in dots aren't points referenced from Rhino and were left out. Connect a Point "
         "parameter with points set from Rhino, and set dots' type hint to Guid." % not_points)
unnamed = sum(1 for _, _, name in points if not name)
if unnamed:
    warn("%d point(s) in dots have no name, so no dot: give them one in Rhino (Properties > Name)." % unnamed)
to_m = to_metres()
dots = g_dots + [make_dot(name, location, to_m) for _, location, name in points if name]
memory["dot_ids"] = set(str(i) for i, _, _ in points)
memory["preview"] = [(location, name, DOT_INK, System.Drawing.Color.White) if name else
                     (location, "no name", NO_NAME, System.Drawing.Color.White) for _, location, name in points]
install_dot_preview(memory)

if not 1 <= group_number <= GROUPS:
    warn("Set group to your group number (1-%d)." % GROUPS, error=True)
    status = "Set group to your group number (1-%d)." % GROUPS
elif not meshes:
    status = "Connect your cube (Breps, meshes or SubDs) to G."
else:
    signature = fingerprint(meshes, dots, "%d|%s" % (group_number, colour_value))
    wanted = bool(inp("push", False))
    if not wanted and inp("auto", False) and signature != memory["fingerprint"]:
        wait = AUTO_EVERY_S - (time.time() - memory["time"])
        if wait <= 0:
            wanted = True
        else:
            memory["status"] = "Changed: auto-publishing in %d s." % (int(wait) + 1)
            run_again_in(wait)
    if wanted:
        try:
            ok, message = publish(meshes, dots, group_number, colour_value, server_url, workshop_key)
        except Exception as ex:
            ok, message = False, "Publish failed: %s" % ex
        memory["time"] = time.time()
        if ok:
            memory["fingerprint"] = signature
        memory["status"] = message
        if not ok:
            warn(message, error=True)
    status = memory["status"]
