"""MultiRender project cache – run ONCE before parallel rendering.

    qgis_process run prepare_cache.py --PROJECT=… --OUTPUT_PROJECT=… --LAYOUTS=[…] --OPTIONS={…} --RESULT_FILE=…

1. reads the project,
2. copies vector layers from remote/database sources (or all of them – by option)
   used by the printed layouts into one local GeoPackage, optionally only the
   features within the extent of the layout maps,
3. switches those layers to the local copy (style, labels and layer ids unchanged),
4. saves a copy of the project (same file name, cache folder) with the home path
   set to the original folder – relative paths and @project_home work as before.

All rendering processes then read this copy, so the server/database is queried once.
Messages are codes with arguments; the plugin translates them.
"""

import json
import os
import re
import time
import traceback

from qgis.core import (
    QgsCoordinateTransform,
    QgsDataProvider,
    QgsLayoutItemMap,
    QgsMapLayer,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputString,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
    QgsProject,
    QgsRectangle,
    QgsVectorFileWriter,
)

try:
    from qgis.core import Qgis
except ImportError:  # pragma: no cover
    Qgis = None

MARK = "@@MR:"


class WorkerError(Exception):
    """Error reported to the plugin as a translatable code."""

    def __init__(self, code, **args):
        super().__init__(f"{code}: {args}" if args else code)
        self.payload = {"c": code, "a": args}


def _enum(owner, scoped, name):
    holder = getattr(owner, scoped, None)
    if holder is not None and hasattr(holder, name):
        return getattr(holder, name)
    return getattr(owner, name)

_LAYER_TYPE = getattr(Qgis, "LayerType", None) if Qgis is not None else None
VECTOR_LAYER = _LAYER_TYPE.Vector if _LAYER_TYPE is not None else QgsMapLayer.VectorLayer

REMOTE_PROVIDERS = {
    "postgres", "wfs", "oapif", "oracle", "mssql", "hana", "arcgisfeatureserver",
    "db2", "sapHana", "mysql",
}
SKIP_PROVIDERS = {"memory", "virtual"}
REMOTE_PREFIXES = ("http://", "https://", "/vsicurl", "/vsis3", "/vsigs", "/vsiaz", "pg:", "mssql:", "oci:", "mysql:")


def _is_remote(layer):
    provider = layer.providerType()
    if provider in REMOTE_PROVIDERS:
        return True
    src = layer.source().strip().strip('"').lower()
    if provider in ("ogr", "delimitedtext", "spatialite"):
        return src.startswith(REMOTE_PREFIXES) or "url=" in src or "file=http" in src
    return False


def should_cache(layer, mode):
    """Whether a vector layer should be copied into the cache."""
    if layer.providerType() in SKIP_PROVIDERS:
        return False
    return mode == "all_vector" or _is_remote(layer)


def _table_name(layer, used):
    base = re.sub(r"[^0-9A-Za-z_]+", "_", layer.name()).strip("_").lower() or "layer"
    base = base[:50]
    name, n = base, 1
    while name in used:
        n += 1
        name = f"{base}_{n}"
    used.add(name)
    return name


def print_layouts(project):
    manager = project.layoutManager()
    try:
        return list(manager.printLayouts())
    except AttributeError:
        return [lay for lay in manager.layouts() if hasattr(lay, "atlas")]


def used_layer_ids(project, layouts):
    """Ids of layers drawn in the layouts (plus layers they join), or None (= cannot tell, cache all)."""
    try:
        from qgis.core import QgsLayoutObject

        ddp = getattr(QgsLayoutObject, "DataDefinedProperty", QgsLayoutObject)
        dynamic = [getattr(ddp, n) for n in ("MapStylePreset", "MapLayers") if hasattr(ddp, n)]

        visible = None  # layers checked in the layer panel – computed once
        used = set()
        for layout in layouts:
            atlas = layout.atlas()
            if atlas is not None and atlas.enabled() and atlas.coverageLayer() is not None:
                used.add(atlas.coverageLayer().id())
            for item in layout.items():
                if not isinstance(item, QgsLayoutItemMap):
                    continue
                props = item.dataDefinedProperties()
                if any(props.isActive(p) for p in dynamic):
                    return None  # theme/layers from an expression – unknown in advance
                if item.keepLayerSet():
                    layers = item.layers()
                elif item.followVisibilityPreset():
                    layers = project.mapThemeCollection().mapThemeVisibleLayers(item.followVisibilityPresetName())
                else:
                    if visible is None:
                        visible = [n.layer() for n in project.layerTreeRoot().findLayers() if n.isVisible()]
                    layers = visible
                used.update(lyr.id() for lyr in layers if lyr is not None)
            for frame in layout.multiFrames():  # attribute tables
                source = getattr(frame, "sourceLayer", None) or getattr(frame, "vectorLayer", None)
                lyr = source() if callable(source) else None
                if lyr is not None:
                    used.add(lyr.id())
        # layers joined to used layers are needed as well (joined attributes in labels/styles)
        pending = list(used)
        while pending:
            lyr = project.mapLayer(pending.pop())
            for join in (lyr.vectorJoins() if lyr is not None and hasattr(lyr, "vectorJoins") else []):
                jid = join.joinLayerId()
                if jid and jid not in used:
                    used.add(jid)
                    pending.append(jid)
        return used
    except Exception:  # noqa: BLE001 – when in doubt it is safer to cache everything
        return None


def layout_extents(project, layouts):
    """List of (rectangle, crs) covering all maps of the layouts."""
    out = []
    for layout in layouts:
        atlas = layout.atlas()
        cov = atlas.coverageLayer() if atlas is not None and atlas.enabled() else None
        for item in layout.items():
            if not isinstance(item, QgsLayoutItemMap):
                continue
            crs = item.crs()
            rect = QgsRectangle(item.extent())
            if cov is not None and item.atlasDriven():
                try:
                    ct = QgsCoordinateTransform(cov.crs(), crs, project)
                    cov_rect = ct.transformBoundingBox(cov.extent())
                    # margin for atlas margins / fixed scales
                    cov_rect.grow(max(cov_rect.width(), cov_rect.height(), rect.width(), rect.height()) * 0.25)
                    rect.combineExtentWith(cov_rect)
                except Exception:  # noqa: BLE001
                    return []  # cannot compute – no filter
            if not rect.isEmpty():
                out.append((rect, crs))
    return out


def extent_for_layer(project, layer, extents):
    """Union of the layout map extents in the CRS of the layer (+5 %), or None (= whole layer)."""
    if not extents:
        return None
    total, first = QgsRectangle(), True
    for rect, crs in extents:
        try:
            r = QgsCoordinateTransform(crs, layer.crs(), project).transformBoundingBox(rect)
        except Exception:  # noqa: BLE001
            return None
        if first:
            total, first = QgsRectangle(r), False
        else:
            total.combineExtentWith(r)
    if first or total.isEmpty():
        return None
    total.grow(max(total.width(), total.height()) * 0.05)
    return total


class MultiRenderPrepareCacheAlgorithm(QgsProcessingAlgorithm):
    def name(self):
        return "multirenderpreparecache"

    def displayName(self):  # noqa: N802
        return "MultiRender – project cache"

    def createInstance(self):  # noqa: N802
        return MultiRenderPrepareCacheAlgorithm()

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile("PROJECT", "Project"))
        self.addParameter(QgsProcessingParameterString("OUTPUT_PROJECT", "Cached project"))
        self.addParameter(QgsProcessingParameterString("LAYOUTS", "Printed layouts (JSON list, empty = all)",
                                                       defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterString("OPTIONS", "Options (JSON)", defaultValue="{}", optional=True))
        self.addParameter(QgsProcessingParameterString("RESULT_FILE", "Result file", defaultValue="", optional=True))
        self.addOutput(QgsProcessingOutputString("CACHE_PROJECT", "Cached project"))

    def _mark(self, kind, code=None, **args):
        value = args.get("value") if code is None else json.dumps({"c": code, "a": args}, ensure_ascii=False)
        print(f"{MARK}{kind}:{value}", flush=True)

    def _progress(self, v):
        v = int(max(0, min(100, v)))
        self._feedback.setProgress(v)
        self._mark("progress", value=v)

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        self._feedback = feedback
        result_file = self.parameterAsString(parameters, "RESULT_FILE", context)
        result = {"ok": False, "files": [], "warnings": [], "error": None, "cached": 0, "skipped": 0, "bytes": 0}
        try:
            out = self._run(parameters, context, result)
            result["ok"] = True
            result["files"] = [out]
        except Exception as e:  # noqa: BLE001
            payload = e.payload if isinstance(e, WorkerError) else {"c": "raw", "a": {"text": str(e) or type(e).__name__}}
            result["error"] = payload
            result["traceback"] = traceback.format_exc()
            raise QgsProcessingException(str(e))
        finally:
            if result_file:
                try:
                    with open(result_file, "w", encoding="utf-8") as fh:
                        json.dump(result, fh, ensure_ascii=False)
                except OSError:
                    pass
        return {"CACHE_PROJECT": result["files"][0] if result["files"] else ""}

    # ------------------------------------------------------------------
    def _run(self, parameters, context, result):
        t0 = time.time()
        src = self.parameterAsFile(parameters, "PROJECT", context)
        dst = self.parameterAsString(parameters, "OUTPUT_PROJECT", context)
        try:
            opts = json.loads(self.parameterAsString(parameters, "OPTIONS", context) or "{}")
        except ValueError:
            opts = {}
        try:
            wanted = set(json.loads(self.parameterAsString(parameters, "LAYOUTS", context) or "[]"))
        except ValueError:
            wanted = set()
        mode = opts.get("cache_mode", "remote")
        use_extent = bool(opts.get("cache_extent_filter", True))

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        gpkg = os.path.splitext(dst)[0] + "_data.gpkg"
        if os.path.exists(gpkg):
            os.remove(gpkg)

        self._progress(1)
        self._mark("stage", "stage_load")
        project = QgsProject.instance()
        flags = None
        pf = getattr(Qgis, "ProjectReadFlag", None) if Qgis is not None else None
        if pf is not None and hasattr(pf, "DontLoad3DViews"):
            flags = pf.DontLoad3DViews
        ok = project.read(src, flags) if flags is not None else project.read(src)
        if not ok:
            raise WorkerError("project_load", error=project.error())
        self._progress(10)

        self._mark("stage", "stage_layouts")
        # only the layouts that are going to be printed (empty list = all)
        layouts = print_layouts(project)
        if wanted:
            layouts = [lay for lay in layouts if lay.name() in wanted]
        extents = layout_extents(project, layouts) if use_extent else []

        used = used_layer_ids(project, layouts)
        if used is None:
            self._mark("info", "cache_dynamic")
        candidates = []
        for layer in project.mapLayers().values():
            if layer.type() != VECTOR_LAYER or not layer.isValid():
                continue
            if used is not None and layer.id() not in used:
                continue  # not drawn in any printed layout – keeps its original source
            if should_cache(layer, mode):
                candidates.append(layer)

        used_names = set()
        total = max(1, len(candidates))
        for i, layer in enumerate(candidates):
            table = _table_name(layer, used_names)
            self._mark("stage", "stage_copy", name=layer.name())
            error = self._copy_layer(project, layer, gpkg, table, extents)
            if error is None:
                result["cached"] += 1
                self._mark("info", "cache_layer", name=layer.name(), count=layer.featureCount())
            else:
                result["skipped"] += 1
                result["warnings"].append(error)
                self._mark("warning", error["c"], **error["a"])
            self._progress(10 + 85 * (i + 1) / total)

        self._mark("stage", "stage_save")
        # project copy: the home path points to the original folder
        project.setPresetHomePath(os.path.dirname(os.path.abspath(src)))
        if not project.write(dst):
            raise WorkerError("cache_save_error", error=project.error())
        result["bytes"] = sum(os.path.getsize(f) for f in (gpkg, dst) if os.path.isfile(f))
        result["seconds"] = round(time.time() - t0, 1)
        self._progress(100)
        return dst

    # ------------------------------------------------------------ copy
    def _copy_layer(self, project, layer, gpkg, table, extents):
        """Copies the layer to the GeoPackage and switches it there. Returns None or an error payload."""
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = table
        opts.fileEncoding = "UTF-8"
        action = "CreateOrOverwriteLayer" if os.path.exists(gpkg) else "CreateOrOverwriteFile"
        opts.actionOnExistingFile = _enum(QgsVectorFileWriter, "ActionOnExistingFile", action)
        extent = extent_for_layer(project, layer, extents)
        if extent is not None:
            opts.filterExtent = extent

        res = QgsVectorFileWriter.writeAsVectorFormatV3(layer, gpkg, project.transformContext(), opts)
        err, msg = res[0], res[1] if len(res) > 1 else ""
        if err != _enum(QgsVectorFileWriter, "WriterError", "NoError"):
            return {"c": "cache_write_error", "a": {"name": layer.name(), "detail": msg or str(err)}}

        uri = f"{gpkg}|layername={table}"
        subset = layer.subsetString()
        old_source, old_provider = layer.source(), layer.providerType()

        def switch(source, provider):
            try:
                layer.setDataSource(source, layer.name(), provider, QgsDataProvider.ProviderOptions())
            except TypeError:
                layer.setDataSource(source, layer.name(), provider)

        switch(uri, "ogr")
        if not layer.isValid():
            switch(old_source, old_provider)  # restore the original
            return {"c": "cache_open_error", "a": {"name": layer.name()}}
        if subset:
            layer.setSubsetString("")  # the filter was applied while copying
        return None
