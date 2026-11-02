"""Project cache built inside the running QGIS session.

Layers of the current project are already loaded and authenticated here (master
password, logins typed at connection, environment of the session) – unlike in
separate qgis_process processes, which may not reach them at all. So:

1. on the main thread: which layers the printed layouts use, their extents and
   thread-safe feature sources of those layers,
2. in a background task (QgsTask): the features are written into one local
   GeoPackage; remote layers not used by the printed layouts can be written as
   empty tables, so that the rendering processes do not try to connect to them,
3. on the main thread: a second, separate QgsProject reads the saved project file
   without loading layers, the copied layers are switched to the GeoPackage and
   the copy is written to the cache folder (home path = original folder, so
   relative paths work as before). The open project is not touched.
"""

import os
import re
import time
import zipfile

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsDataProvider,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsProject,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayerFeatureSource,
)
from qgis.PyQt.QtCore import QObject, pyqtSignal

from .i18n import tr
from .worker.prepare_cache import (
    VECTOR_LAYER,
    _enum,
    _is_remote,
    _table_name,
    extent_for_layer,
    layout_extents,
    print_layouts,
    should_cache,
    used_layer_ids,
)

FID_COLUMN = "multirender_fid"  # avoids clashes with a source column called "fid"
# field types a GeoPackage stores directly (QMetaType ids, equal to the QVariant ids in Qt5):
# Bool, Int, UInt, LongLong, ULongLong, Double, QString, QStringList, QByteArray, QDate, QTime, QDateTime
PLAIN_TYPES = {1, 2, 3, 4, 5, 6, 10, 11, 12, 14, 15, 16}


def _type_id(field):
    t = field.type()
    return int(getattr(t, "value", t))


def _as_text_field(field):
    """Copy of the field as text – for types a GeoPackage cannot store (e.g. a second geometry column)."""
    out = QgsField(field)
    try:
        from qgis.PyQt.QtCore import QMetaType

        out.setType(QMetaType.Type.QString)
    except (ImportError, AttributeError, TypeError):
        from qgis.PyQt.QtCore import QVariant

        out.setType(QVariant.String)
    out.setTypeName("String")
    out.setLength(0)
    out.setPrecision(0)
    return out


def _as_text(value):
    if value is None or (hasattr(value, "isNull") and value.isNull()):
        return None
    if hasattr(value, "asWkt"):  # QgsGeometry
        return value.asWkt()
    return str(value)


def _provider_field_indexes(fields):
    """Indexes of fields stored in the source (joined and virtual fields are recreated from the project)."""
    origin = getattr(getattr(Qgis, "FieldOrigin", None), "Provider", None)
    if origin is None:
        origin = QgsFields.OriginProvider
    return [i for i in range(fields.count()) if fields.fieldOrigin(i) == origin]


def project_xml(path):
    """Text of the project file (.qgs or .qgz) – used to find layers referenced by expressions."""
    try:
        if path.lower().endswith(".qgz"):
            with zipfile.ZipFile(path) as zf:
                name = next(n for n in zf.namelist() if n.lower().endswith(".qgs"))
                return zf.read(name).decode("utf-8", "replace")
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return ""


def referenced_in_expressions(xml, layer):
    """Whether the layer name or id appears as a quoted string ('…') – the way expressions
    (aggregate, get_feature, overlay_* …) refer to other layers."""
    for value in {layer.name(), layer.id()}:
        if not value:
            continue
        esc = re.escape(value)
        if re.search(rf"(?:'|&apos;){esc}(?:'|&apos;)", xml):
            return True
    return False


def _copy_task(task, items, gpkg, transform_context):
    """Background part: writes all items into the GeoPackage. Touches no project/layer objects."""
    t0 = time.time()
    no_error = _enum(QgsVectorFileWriter, "WriterError", "NoError")
    create_file = _enum(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteFile")
    create_layer = _enum(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteLayer")
    done, errors, file_exists = [], [], False
    for n, item in enumerate(items):
        if task.isCanceled():
            return None
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = item["table"]
        options.fileEncoding = "UTF-8"
        options.layerOptions = [f"FID={FID_COLUMN}"]
        options.actionOnExistingFile = create_layer if file_exists else create_file
        writer = QgsVectorFileWriter.create(gpkg, item["fields"], item["wkb"], item["crs"], transform_context, options)
        if writer is None or writer.hasError() != no_error:
            errors.append({"c": "cache_write_error",
                           "a": {"name": item["name"], "detail": writer.errorMessage() if writer else "?"}})
            continue
        file_exists = True
        count = 0
        if not item["empty"]:
            request = QgsFeatureRequest()
            if item["rect"] is not None:
                request.setFilterRect(item["rect"])
            request.setSubsetOfAttributes(item["indexes"])
            indexes, text = item["indexes"], item["text"]
            for feature in item["source"].getFeatures(request):
                if task.isCanceled():
                    break
                out = QgsFeature(item["fields"])
                out.setGeometry(feature.geometry())
                values = feature.attributes()
                out.setAttributes([_as_text(values[i]) if i in text else values[i] for i in indexes])
                writer.addFeature(out)
                count += 1
        del writer  # closes the table
        done.append({"id": item["id"], "name": item["name"], "table": item["table"], "count": count,
                     "empty": item["empty"]})
        task.setProgress(100.0 * (n + 1) / len(items))
    return {"done": done, "errors": errors, "seconds": time.time() - t0}


class LiveCache(QObject):
    """Builds the cache of the current project; emits finished(ok) on the main thread."""

    progress = pyqtSignal(float)
    finished = pyqtSignal(bool)

    def __init__(self, project_path, cache_project, layouts, options, parent=None):
        super().__init__(parent)
        self.project_path = project_path
        self.cache_project = cache_project
        self.gpkg = os.path.splitext(cache_project)[0] + "_data.gpkg"
        self.layouts = set(layouts)
        self.options = dict(options)
        self.result = {}
        self.render_jobs = []
        self.started = self.finished_at = None
        self.percent = 0.0
        self.dynamic = False
        self._task = None
        self._items = []

    @property
    def label(self):
        return tr("Cache: {name}", name=os.path.basename(self.project_path))

    @property
    def duration(self):
        return (self.finished_at or time.time()) - (self.started or time.time())

    # ---------------------------------------------------------- main thread
    def prepare(self):
        """Collects the work. Returns the number of layers to write (0 = nothing to cache)."""
        project = QgsProject.instance()
        layouts = [lay for lay in print_layouts(project) if lay.name() in self.layouts] or print_layouts(project)
        mode = self.options.get("cache_mode", "remote")
        extents = layout_extents(project, layouts) if self.options.get("cache_extent_filter", True) else []
        used = used_layer_ids(project, layouts)
        self.dynamic = used is None  # layers chosen by expressions – everything matching is cached
        skip_unused = bool(self.options.get("cache_skip_unused", True)) and used is not None
        xml = project_xml(self.project_path)
        names = set()
        self._items = []
        for layer in project.mapLayers().values():
            if layer.type() != VECTOR_LAYER or not layer.isValid():
                continue
            in_use = used is None or layer.id() in used
            referenced = referenced_in_expressions(xml, layer)
            if (in_use or referenced) and should_cache(layer, mode):
                # an expression may aggregate over the whole layer – no extent filter then
                rect = None if referenced else extent_for_layer(project, layer, extents)
                self._items.append(self._item(layer, names, rect, empty=False))
            elif skip_unused and not in_use and not referenced and _is_remote(layer):
                self._items.append(self._item(layer, names, None, empty=True))
        return len(self._items)

    def _item(self, layer, names, rect, empty):
        fields = layer.fields()
        indexes = _provider_field_indexes(fields)
        out_fields, text = QgsFields(), set()
        for i in indexes:
            field = fields.at(i)
            if _type_id(field) in PLAIN_TYPES:
                out_fields.append(field)
            else:  # e.g. an extra geometry column of a PostGIS table → stored as WKT text
                out_fields.append(_as_text_field(field))
                text.add(i)
        return {
            "id": layer.id(), "name": layer.name(), "table": _table_name(layer, names),
            "source": None if empty else QgsVectorLayerFeatureSource(layer),
            "fields": out_fields, "indexes": indexes, "text": text, "wkb": layer.wkbType(), "crs": layer.crs(),
            "rect": rect, "empty": empty,
        }

    def start(self):
        self.started = time.time()
        os.makedirs(os.path.dirname(self.cache_project), exist_ok=True)
        if os.path.exists(self.gpkg):
            os.remove(self.gpkg)
        ctx = QgsProject.instance().transformContext()
        self._task = QgsTask.fromFunction(
            tr("MultiRender – project cache"), _copy_task, self._items, self.gpkg, ctx, on_finished=self._done,
        )
        self._task.progressChanged.connect(self._on_progress)
        QgsApplication.taskManager().addTask(self._task)

    def _on_progress(self, value):
        self.percent = float(value)
        self.progress.emit(self.percent)

    def cancel(self):
        if self._task is not None:
            self._task.cancel()

    def _done(self, exception, result=None):
        self._task = None
        self._items = []  # releases the feature sources
        self.percent = 100.0
        ok = False
        try:
            if exception is not None:
                self.result = {"error": str(exception)}
            elif result is None:
                self.result = {"canceled": True}  # the task was canceled
            else:
                ok = self._write_project(result)
        except Exception as e:  # noqa: BLE001
            self.result = {"error": str(e) or type(e).__name__}
        self.finished_at = time.time()
        self.finished.emit(ok)

    def _write_project(self, result):
        """Second QgsProject: read without layers, switch the copied layers, save the copy."""
        flags = None
        pf = getattr(Qgis, "ProjectReadFlag", None)
        if pf is not None:
            for name in ("DontResolveLayers", "DontLoad3DViews"):
                flag = getattr(pf, name, None)
                if flag is not None:
                    flags = flag if flags is None else flags | flag
        copy = QgsProject()
        try:
            if not (copy.read(self.project_path, flags) if flags is not None else copy.read(self.project_path)):
                self.result = {"error": copy.error()}
                return False
            switched, failed = 0, list(result["errors"])
            for item in result["done"]:
                layer = copy.mapLayer(item["id"])
                if layer is None:
                    continue
                if self._switch(layer, f"{self.gpkg}|layername={item['table']}"):
                    switched += 1
                else:
                    failed.append({"c": "cache_open_error", "a": {"name": item["name"]}})
            copy.setPresetHomePath(os.path.dirname(os.path.abspath(self.project_path)))
            if not copy.write(self.cache_project):
                self.result = {"error": copy.error()}
                return False
        finally:
            copy.clear()
        done = result["done"]
        self.result = {
            "cached": sum(1 for d in done if not d["empty"]),
            "placeholders": sum(1 for d in done if d["empty"]),
            "skipped": len(failed), "warnings": failed, "switched": switched,
            "bytes": sum(os.path.getsize(f) for f in (self.gpkg, self.cache_project) if os.path.isfile(f)),
            "seconds": result["seconds"],
        }
        return True

    @staticmethod
    def _switch(layer, uri):
        old_source, old_provider = layer.source(), layer.providerType()

        def set_source(source, provider):
            try:
                layer.setDataSource(source, layer.name(), provider, QgsDataProvider.ProviderOptions())
            except TypeError:
                layer.setDataSource(source, layer.name(), provider)

        set_source(uri, "ogr")
        if not layer.isValid():
            set_source(old_source, old_provider)
            return False
        if layer.subsetString():
            layer.setSubsetString("")  # the filter of the source was applied while copying
        return True
