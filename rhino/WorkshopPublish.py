# -*- coding: utf-8 -*-
"""
Workshop Live: publish your group's cube to the class screen.

Run it from a toolbar button or alias:
    ! _-RunPythonScript "C:\\path\\to\\WorkshopPublish.py"

First run asks which group you are (Group 1..8). The choice is saved in the
Rhino file. Then select the ONE object that is your cube: only that object is
published. Press Enter instead of selecting to publish the same object as
last time.

Your cube has to be one object: join its parts into one polysurface or mesh,
or make a block of them. Surfaces, polysurfaces, extrusions, SubDs and meshes
work; blocks are exploded into meshes when published.

Options on the command line let you change the group number, turn on
"PublishOnSave", which publishes the object you published last every time you
press Ctrl+S, and turn on "TextDots", which publishes the text dots inside your
cube along with it (dots within its bounding box, or up to 3 cm outside it, and
dots that are part of it when it's a block). They show on the class screen as
labels.

"NegativeSpace" lets you pick the objects that cut your cube's voids (the
cutters of a BooleanDifference, say). They're published with every publish
after that, on a layer of their own, and the class screen can show them very
ghosted (N). They can be hidden in Rhino once picked; pick again with nothing
selected (Enter) to stop publishing them.

Each group publishes as its number ("1".."8"), which is the group chip with
the same number on the class screen. Geometry is converted to meters, so
files in millimetres or centimetres line up with everyone else's.

Works in Rhino 7 and Rhino 8 (Windows and Mac). No plug-ins required.
"""
import os
import json
import System
import Rhino
import Rhino.Geometry as rg
import scriptcontext as sc

# ---------------------------------------------------------------------------
# Instructor: set these two lines before handing the script out.
SERVER_URL = "https://tainanrocks.vercel.app"
WORKSHOP_KEY = "blocks-2026"
# ---------------------------------------------------------------------------

GROUPS = 8          # number of groups; keep it the same as ?groups= on the class screen
SECTION = "WorkshopLive"
STICKY_HANDLER = "workshoplive_publish_on_save"
MAX_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 8  # nested block definitions
DOT_MARGIN_M = 0.03   # text dots this far outside the cube's bounding box still count as on it
CUBE_LAYER = "Cube"
NEGATIVE_LAYER = "Negative space"   # negative space goes on this layer, for anyone opening the file...
NEGATIVE_TAG = ("workshoplive", "negative")   # ... and is marked with this user text, which is what counts


# ---------------------------------------------------------------- settings
def get_setting(doc, key, default=None):
    value = doc.Strings.GetValue(SECTION, key)
    return value if value else default


def set_setting(doc, key, value):
    doc.Strings.SetString(SECTION, key, value)


def saved_group(doc):
    """The group number saved in this file, or None (also for files that still hold an old A1..D4 block)."""
    value = get_setting(doc, "group")
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= GROUPS else None


def pick_group(current):
    import rhinoscriptsyntax as rs
    names = ["Group %d" % n for n in range(1, GROUPS + 1)]
    choice = rs.ListBox(names, "Which group are you?", "Workshop Live",
                        names[current - 1] if current else None)
    return names.index(choice) + 1 if choice else None


def saved_object(doc):
    """The object published last time from this file, if it still exists."""
    value = get_setting(doc, "object")
    if not value:
        return None
    try:
        obj = doc.Objects.FindId(System.Guid.Parse(value))
    except Exception:
        return None
    return obj if obj is not None and not obj.IsDeleted else None


def saved_negative(doc):
    """The objects picked as negative space that still exist (hidden ones too), and how many are gone."""
    found, gone = [], 0
    for value in (get_setting(doc, "negative") or "").split(","):
        if not value.strip():
            continue
        try:
            obj = doc.Objects.FindId(System.Guid.Parse(value.strip()))
        except Exception:
            obj = None
        if obj is None or obj.IsDeleted:
            gone += 1
        else:
            found.append(obj)
    return found, gone


def pick_negative(doc):
    """Ask for the objects that cut the cube's voids and save them in the file. Enter = none."""
    go = Rhino.Input.Custom.GetObject()
    go.SetCommandPrompt("Select the objects that cut your cube's voids (Enter = none)")
    go.GeometryFilter = (Rhino.DocObjects.ObjectType.Surface | Rhino.DocObjects.ObjectType.PolysrfFilter |
                         Rhino.DocObjects.ObjectType.Mesh | Rhino.DocObjects.ObjectType.Extrusion |
                         Rhino.DocObjects.ObjectType.SubD | Rhino.DocObjects.ObjectType.InstanceReference)
    go.EnablePreSelect(False, True)
    go.SubObjectSelect = False
    go.GroupSelect = True
    go.AcceptNothing(True)
    res = go.GetMultiple(1, 0)
    if res == Rhino.Input.GetResult.Nothing:
        ids = []
    elif res == Rhino.Input.GetResult.Object:
        ids = [str(go.Object(i).ObjectId) for i in range(go.ObjectCount)]
    else:
        return   # Escape: keep what was there
    set_setting(doc, "negative", ",".join(ids))
    doc.Objects.UnselectAll()
    doc.Views.Redraw()
    Rhino.RhinoApp.WriteLine("Workshop Live: %s." % (
        "%d object%s published as negative space from now on" % (len(ids), "" if len(ids) == 1 else "s")
        if ids else "no negative space published from now on"))


# ---------------------------------------------------------------- geometry
def meshing_parameters(doc):
    try:
        return doc.GetMeshingParameters(doc.MeshingParameterStyle)
    except Exception:
        return rg.MeshingParameters.Default


def to_meshes(geom, mp):
    """Return a list of meshes for any surface-like geometry, else []."""
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


def collect_meshes(doc, geom, xform, color, mp, out, depth=0):
    """Append (mesh, color) pairs for the geometry (blocks are exploded) to `out`."""
    if isinstance(geom, rg.InstanceReferenceGeometry):
        if depth >= MAX_DEPTH:
            return
        idef = doc.InstanceDefinitions.FindId(geom.ParentIdefId)
        if idef is None:
            return
        child_xform = xform * geom.Xform
        for child in idef.GetObjects():
            collect_meshes(doc, child.Geometry, child_xform, child.Attributes.DrawColor(doc), mp, out, depth + 1)
        return
    meshes = to_meshes(geom, mp)
    if not meshes:
        return
    joined = rg.Mesh()
    for m in meshes:
        if m is not None:
            joined.Append(m)
    if joined.Faces.Count == 0:
        return
    joined.Transform(xform)
    joined.Normals.ComputeNormals()
    joined.Compact()
    out.append((joined, color))


def moved_dot(dot, xform):
    """A copy of a text dot with its point transformed (the text keeps its size on screen)."""
    copy = rg.TextDot(dot.Text, xform * dot.Point)
    try:
        copy.FontHeight = dot.FontHeight
        copy.FontFace = dot.FontFace
    except Exception:
        pass
    return copy


def dots_in_block(doc, geom, xform, out, depth=0):
    """Append (dot, color) for every text dot inside a block (and the blocks inside it)."""
    if not isinstance(geom, rg.InstanceReferenceGeometry) or depth >= MAX_DEPTH:
        return
    idef = doc.InstanceDefinitions.FindId(geom.ParentIdefId)
    if idef is None:
        return
    child_xform = xform * geom.Xform
    for child in idef.GetObjects():
        if isinstance(child.Geometry, rg.TextDot):
            out.append((moved_dot(child.Geometry, child_xform), child.Attributes.DrawColor(doc)))
        else:
            dots_in_block(doc, child.Geometry, child_xform, out, depth + 1)


def collect_dots(doc, obj, to_m):
    """The text dots that go with the cube, in metres: the visible dots in the file that sit inside
    its bounding box (or up to DOT_MARGIN_M outside it), and the dots inside it if it's a block."""
    scale = rg.Transform.Scale(rg.Point3d.Origin, to_m)
    out = []
    dots_in_block(doc, obj.Geometry, scale, out)
    box = obj.Geometry.GetBoundingBox(True)
    if not box.IsValid:
        return out
    pad = DOT_MARGIN_M / to_m
    box = rg.BoundingBox(rg.Point3d(box.Min.X - pad, box.Min.Y - pad, box.Min.Z - pad),
                         rg.Point3d(box.Max.X + pad, box.Max.Y + pad, box.Max.Z + pad))
    settings = Rhino.DocObjects.ObjectEnumeratorSettings()
    settings.ObjectTypeFilter = Rhino.DocObjects.ObjectType.TextDot
    settings.HiddenObjects = False
    settings.LockedObjects = True
    settings.VisibleFilter = True
    for dot_obj in doc.Objects.GetObjectList(settings):
        dot = dot_obj.Geometry
        if isinstance(dot, rg.TextDot) and box.Contains(dot.Point):
            out.append((moved_dot(dot, scale), dot_obj.Attributes.DrawColor(doc)))
    return out


def add_dot(f, dot, attr):
    try:
        return f.Objects.AddTextDot(dot, attr)
    except Exception:
        return f.Objects.AddTextDot(dot.Text, dot.Point, attr)


def add_layer(f, name, colour):
    """Add a layer to the file being written; returns its index."""
    try:
        return f.AllLayers.AddLayer(name, colour)
    except Exception:
        layer = Rhino.DocObjects.Layer()
        layer.Name = name
        layer.Color = colour
        f.AllLayers.Add(layer)
        return f.AllLayers.Count - 1


def build_file(doc, obj, group, with_dots=False, negative=()):
    """Write the one selected object, as meshes in metres, to a temporary .3dm, with the text dots on
    it if with_dots, and the `negative` objects (meshed) on the negative-space layer. Returns
    (path, meshes, dots, negative meshes)."""
    to_m = Rhino.RhinoMath.UnitScale(doc.ModelUnitSystem, Rhino.UnitSystem.Meters)
    to_m_xform = rg.Transform.Scale(rg.Point3d.Origin, to_m)
    mp = meshing_parameters(doc)
    parts = []
    collect_meshes(doc, obj.Geometry, to_m_xform, obj.Attributes.DrawColor(doc), mp, parts)
    dots = collect_dots(doc, obj, to_m) if with_dots and parts else []
    ghosts = []
    for other in negative:
        if other.Id != obj.Id:
            collect_meshes(doc, other.Geometry, to_m_xform, other.Attributes.DrawColor(doc), mp, ghosts)
    f = Rhino.FileIO.File3dm()
    f.Settings.ModelUnitSystem = Rhino.UnitSystem.Meters
    # The cube gets a layer of its own, made first: objects land on a file's first layer unless told
    # otherwise, and that must never be the negative-space layer.
    cube_layer = add_layer(f, CUBE_LAYER, System.Drawing.Color.Black)
    if ghosts:
        layer = add_layer(f, NEGATIVE_LAYER, System.Drawing.Color.FromArgb(160, 160, 160))
        for mesh, color in ghosts:
            attr = Rhino.DocObjects.ObjectAttributes()
            attr.LayerIndex = layer
            attr.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
            attr.ObjectColor = color
            attr.SetUserString(NEGATIVE_TAG[0], NEGATIVE_TAG[1])
            f.Objects.AddMesh(mesh, attr)
    for geometry, color in parts + dots:
        attr = Rhino.DocObjects.ObjectAttributes()
        attr.LayerIndex = cube_layer
        attr.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
        attr.ObjectColor = color
        if isinstance(geometry, rg.TextDot):
            add_dot(f, geometry, attr)
        else:
            f.Objects.AddMesh(geometry, attr)
    path = os.path.join(System.IO.Path.GetTempPath(), "workshoplive_group%d.3dm" % group)
    if not f.Write(path, 7):
        raise Exception("could not write the temporary file %s" % path)
    return path, len(parts), len(dots), len(ghosts)


# ---------------------------------------------------------------- network
def http_send(method, url, data=None):
    try:
        System.Net.ServicePointManager.SecurityProtocol = (
            System.Net.ServicePointManager.SecurityProtocol | System.Net.SecurityProtocolType.Tls12)
    except Exception:
        pass
    wc = System.Net.WebClient()
    wc.Headers.Add("x-workshop-key", WORKSHOP_KEY)
    wc.Headers.Add("Content-Type", "application/octet-stream")
    try:
        if data is None:
            data = System.Array.CreateInstance(System.Byte, 0)
        raw = wc.UploadData(url, method, data)
        return 200, System.Text.Encoding.UTF8.GetString(raw)
    except System.Net.WebException as ex:
        if ex.Response is None:
            raise Exception("can't reach %s (%s)" % (SERVER_URL, ex.Message))
        status = System.Convert.ToInt32(ex.Response.StatusCode)
        reader = System.IO.StreamReader(ex.Response.GetResponseStream())
        return status, reader.ReadToEnd()


def publish(doc, group, obj):
    """Publish one object as the group's cube. Returns (ok, message)."""
    if "your-project" in SERVER_URL.lower():
        return False, "The instructor still needs to set SERVER_URL at the top of WorkshopPublish.py."
    Rhino.RhinoApp.WriteLine("Workshop Live: preparing Group %d..." % group)
    with_dots = get_setting(doc, "textdots") == "1"
    negative, gone = saved_negative(doc)
    if gone:
        Rhino.RhinoApp.WriteLine("Workshop Live: %d negative-space object%s no longer exist%s and %s left out." % (
            gone, "" if gone == 1 else "s", "s" if gone == 1 else "", "is" if gone == 1 else "are"))
    path, meshes, dots, ghosts = build_file(doc, obj, group, with_dots, negative)
    if meshes == 0:
        return False, ("That object can't be published: select your cube itself (a polysurface, surface, "
                       "extrusion, SubD, mesh or block), not a curve, point or text.")
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        return False, ("Group %d's cube%s is %.1f MB; the limit is 4 MB. Simplify very smooth surfaces "
                       "or fine detail and publish again." % (group, " with its negative space" if ghosts else "",
                                                             size / 1048576.0))

    data = System.IO.File.ReadAllBytes(path)
    url = "%s/api/publish?block=%d" % (SERVER_URL.rstrip("/"), group)
    status, body = http_send("POST", url, data)
    try:
        result = json.loads(body)
    except Exception:
        result = {"error": body[:200]}
    if status == 200 and result.get("ok"):
        set_setting(doc, "object", str(obj.Id))   # Enter and PublishOnSave reuse it
        on_screen = result.get("blocksPublished")
        extra = ", %d groups on the screen" % on_screen if on_screen else ""
        if with_dots:
            extra = ", %d text dot%s%s" % (dots, "" if dots == 1 else "s", extra)
        if negative:
            extra = ", negative space%s" % extra
        return True, "Published Group %d to the class screen (%.2f MB%s)." % (group, size / 1048576.0, extra)
    return False, "Publish failed (%s): %s" % (status, result.get("error", "unknown error"))


# ---------------------------------------------------------------- publish on save
def on_end_save(sender, e):
    try:
        if getattr(e, "ExportSelected", False) or not getattr(e, "Success", True):
            return
        doc = e.Document
        if doc is None or get_setting(doc, "autopublish") != "1":
            return
        group = saved_group(doc)
        if not group:
            return
        obj = saved_object(doc)
        if obj is None:
            Rhino.RhinoApp.WriteLine("Workshop Live: not published on save: the object you published last is gone. "
                                     "Run the script and select your cube.")
            return
        ok, msg = publish(doc, group, obj)
        Rhino.RhinoApp.WriteLine("Workshop Live: " + msg)
    except Exception as ex:
        Rhino.RhinoApp.WriteLine("Workshop Live: publish on save failed (%s)" % ex)


def install_save_handler():
    old = sc.sticky.get(STICKY_HANDLER)
    if old is not None:
        try:
            Rhino.RhinoDoc.EndSaveDocument -= old
        except Exception:
            pass
    Rhino.RhinoDoc.EndSaveDocument += on_end_save
    sc.sticky[STICKY_HANDLER] = on_end_save


def clear_old_cube_drawing():
    """An earlier version could draw a red box (ShowCube); switch it off if it's still on in this session."""
    state = sc.sticky.get("workshoplive_cube_drawing")
    if state is not None:
        state["box"] = None


# ---------------------------------------------------------------- command
def main():
    doc = sc.doc
    clear_old_cube_drawing()
    group = saved_group(doc)
    if not group:
        group = pick_group(None)
        if not group:
            return
        set_setting(doc, "group", str(group))

    install_save_handler()
    doc.Objects.UnselectAll()   # start from nothing selected, so exactly one object gets picked
    doc.Views.Redraw()

    while True:
        last = saved_object(doc)
        auto = Rhino.Input.Custom.OptionToggle(get_setting(doc, "autopublish") == "1", "Off", "On")
        with_dots = Rhino.Input.Custom.OptionToggle(get_setting(doc, "textdots") == "1", "Off", "On")
        number = Rhino.Input.Custom.OptionInteger(group, 1, GROUPS)
        go = Rhino.Input.Custom.GetObject()
        negative, _ = saved_negative(doc)
        go.SetCommandPrompt("Select your cube to publish as Group %d%s%s" % (
            group, " (Enter = the same object as last time)" if last else "",
            ", with %d negative-space object%s" % (len(negative), "" if len(negative) == 1 else "s") if negative else ""))
        go.EnablePreSelect(False, True)
        go.SubObjectSelect = False
        go.GroupSelect = False
        go.AcceptNothing(last is not None)
        opt_group = go.AddOptionInteger("Group", number)
        opt_auto = go.AddOptionToggle("PublishOnSave", auto)
        opt_dots = go.AddOptionToggle("TextDots", with_dots)
        opt_negative = go.AddOption("NegativeSpace")
        res = go.Get()   # Get (not GetMultiple): exactly one object

        if res == Rhino.Input.GetResult.Option:
            if go.OptionIndex() == opt_group:
                if number.CurrentValue != group:
                    group = number.CurrentValue
                    set_setting(doc, "group", str(group))
                    Rhino.RhinoApp.WriteLine("Workshop Live: this file now publishes as Group %d." % group)
            elif go.OptionIndex() == opt_auto:
                set_setting(doc, "autopublish", "1" if auto.CurrentValue else "0")
                Rhino.RhinoApp.WriteLine("Workshop Live: publish on save is %s." % ("on" if auto.CurrentValue else "off"))
            elif go.OptionIndex() == opt_dots:
                set_setting(doc, "textdots", "1" if with_dots.CurrentValue else "0")
                Rhino.RhinoApp.WriteLine("Workshop Live: text dots on your cube are %s." % (
                    "published with it" if with_dots.CurrentValue else "left out"))
            elif go.OptionIndex() == opt_negative:
                pick_negative(doc)
            continue
        if res == Rhino.Input.GetResult.Nothing and last is not None:
            obj = last
            break
        if res == Rhino.Input.GetResult.Object:
            obj = go.Object(0).Object()
            break
        return  # Escape

    doc.Objects.UnselectAll()
    doc.Views.Redraw()
    try:
        ok, msg = publish(doc, group, obj)
    except Exception as ex:
        ok, msg = False, "Publish failed: %s" % ex
    Rhino.RhinoApp.WriteLine("Workshop Live: " + msg)
    if not ok:
        Rhino.UI.Dialogs.ShowMessage(msg, "Workshop Live")


if __name__ == "__main__":
    main()
