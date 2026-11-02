"""MultiRender worker – exports one print layout (or a part of it).

Processing script run by the plugin as:

    qgis_process run render_layout.py --PROJECT=... --LAYOUT=... --FORMAT=png
                 --DPI=300 --OUTPUT_BASE=... --ATLAS=1 --PAGES=... --PART=1 --PARTS=1
                 --OPTIONS={...} --RESULT_FILE=...

Every call is a separate process, so the project is read into
QgsProject.instance() – there is no conflict with other jobs, and some layout
items (e.g. expressions) refer to the singleton anyway.

With --MODE=profile nothing is exported: the worker measures where the time
goes (project loading, every layer separately – data and drawing –, labels,
other layout items, writing the file, peak memory) and returns the numbers.

The result is written to a JSON file (RESULT_FILE); progress and messages go to
stdout as "@@MR:<kind>:<value>" lines. Messages are codes with arguments
({"c": code, "a": {...}}) – the plugin translates them into the chosen language.
"""

import json
import os
import sys
import tempfile
import time
import traceback

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsLayoutItemMap,
    QgsLayoutMeasurement,
    QgsMapLayer,
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsFeedback,
    QgsLayoutExporter,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProject,
)
from qgis.PyQt.QtCore import QSize, QSizeF

MARK = "@@MR:"
RASTER_FORMATS = {"png": ".png", "jpg": ".jpg", "tif": ".tif", "bmp": ".bmp"}
WORLD_EXT = {".png": (".pgw",), ".jpg": (".jgw",), ".tif": (".tfw",), ".bmp": (".bpw",)}


def parse_pages(spec, total):
    """“2-5,8,10-” → 0-based indexes within 0..total-1 (empty = all)."""
    spec = (spec or "").replace(" ", "")
    if not spec:
        return list(range(total))
    chosen = set()
    for part in spec.split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            start = int(a) if a else 1
            end = int(b) if b else total
        else:
            start = end = int(part)
        chosen.update(range(max(1, start) - 1, min(total, end)))
    return sorted(chosen)


def split_part(items, part, parts):
    """Part `part` (1..parts) of a list split into contiguous, equal chunks."""
    parts = max(1, parts)
    n = len(items)
    return items[(part - 1) * n // parts: part * n // parts]


def _enum(owner, scoped, name):
    """Enum value compatible with QGIS 3.40 (PyQt5) and QGIS 4 (PyQt6)."""
    holder = getattr(owner, scoped, None)
    if holder is not None and hasattr(holder, name):
        return getattr(holder, name)
    return getattr(owner, name)


SUCCESS = _enum(QgsLayoutExporter, "ExportResult", "Success")
_LAYER_TYPE = getattr(Qgis, "LayerType", None)
VECTOR_LAYER = _LAYER_TYPE.Vector if _LAYER_TYPE is not None else QgsMapLayer.VectorLayer
RASTER_LAYER = _LAYER_TYPE.Raster if _LAYER_TYPE is not None else QgsMapLayer.RasterLayer
EXPORT_ERRORS = {
    "Canceled": "export_canceled",
    "MemoryError": "export_memory",
    "FileError": "export_file",
    "PrintError": "export_print",
    "SvgLayerError": "export_svg",
    "IteratorError": "export_iterator",
}


def peak_memory():
    """Peak memory of this process in bytes (0 = unknown)."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (n, ctypes.c_size_t) for n in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            get_info = getattr(kernel32, "K32GetProcessMemoryInfo", None) or ctypes.WinDLL("psapi").GetProcessMemoryInfo
            get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            get_info.restype = wintypes.BOOL
            ok = get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
            return int(counters.PeakWorkingSetSize) if ok else 0
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except Exception:  # noqa: BLE001
        return 0


def total_memory():
    """Physical memory of the computer in bytes (0 = unknown)."""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                    (n, ctypes.c_ulonglong) for n in (
                        "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                        "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]

            status = Status()
            status.dwLength = ctypes.sizeof(Status)
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(Status)]
            kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
            return int(status.ullTotalPhys) if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else 0
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except Exception:  # noqa: BLE001
        return 0


class WorkerError(Exception):
    """Error reported to the plugin as a translatable code."""

    def __init__(self, code, **args):
        super().__init__(f"{code}: {args}" if args else code)
        self.payload = {"c": code, "a": args}


def _export_error(res, err=""):
    detail = f": {err}" if err else ""
    for key, code in EXPORT_ERRORS.items():
        try:
            if res == _enum(QgsLayoutExporter, "ExportResult", key):
                return WorkerError(code, detail=detail)
        except AttributeError:
            pass
    return WorkerError("export_code", code=str(res), detail=detail)


class MultiRenderLayoutAlgorithm(QgsProcessingAlgorithm):
    def name(self):
        return "multirenderlayout"

    def displayName(self):  # noqa: N802
        return "MultiRender – export layout"

    def shortHelpString(self):  # noqa: N802
        return "MultiRender worker. Exports one print layout."

    def createInstance(self):  # noqa: N802
        return MultiRenderLayoutAlgorithm()

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile("PROJECT", "Project"))
        self.addParameter(QgsProcessingParameterString("LAYOUT", "Layout"))
        self.addParameter(QgsProcessingParameterString("FORMAT", "Format", defaultValue="png"))
        self.addParameter(QgsProcessingParameterNumber("DPI", "DPI", defaultValue=300, minValue=1, maxValue=10000))
        self.addParameter(QgsProcessingParameterString("OUTPUT_BASE", "Output path without extension"))
        self.addParameter(QgsProcessingParameterBoolean("ATLAS", "Export atlas", defaultValue=True))
        self.addParameter(QgsProcessingParameterString("OPTIONS", "Options (JSON)", defaultValue="{}", optional=True))
        self.addParameter(QgsProcessingParameterString("PAGES", "Pages (e.g. 2-5,8)", defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterNumber("PART", "Part", defaultValue=1, minValue=1))
        self.addParameter(QgsProcessingParameterNumber("PARTS", "Number of parts", defaultValue=1, minValue=1))
        self.addParameter(QgsProcessingParameterString("MODE", "Mode (render | profile)", defaultValue="render",
                                                       optional=True))
        self.addParameter(QgsProcessingParameterString("RESULT_FILE", "Result file (JSON)", defaultValue="",
                                                       optional=True))
        self.addOutput(QgsProcessingOutputString("OUTPUT_FILES", "Created files"))

    # ------------------------------------------------------------------ run
    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        self._feedback = feedback
        result_file = self.parameterAsString(parameters, "RESULT_FILE", context)
        result = {"ok": False, "files": [], "warnings": [], "error": None}
        self._timing = {}
        try:
            result["files"] = self._run(parameters, context, result)
            result["ok"] = True
        except Exception as e:  # noqa: BLE001
            payload = e.payload if isinstance(e, WorkerError) else {"c": "raw", "a": {"text": str(e) or type(e).__name__}}
            result["error"] = payload
            result["traceback"] = traceback.format_exc()
            raise QgsProcessingException(str(e))
        finally:
            result["timing"] = self._timing
            result["peak_mem"] = peak_memory()
            if result_file:
                try:
                    with open(result_file, "w", encoding="utf-8") as fh:
                        json.dump(result, fh, ensure_ascii=False)
                except OSError:
                    pass
        return {"OUTPUT_FILES": ";".join(result["files"])}

    def _mark(self, kind, code=None, **args):
        value = args.get("value") if code is None else json.dumps({"c": code, "a": args}, ensure_ascii=False)
        print(f"{MARK}{kind}:{value}", flush=True)

    def _progress(self, value):
        value = int(max(0, min(100, value)))
        self._feedback.setProgress(value)
        self._mark("progress", value=value)

    def _run(self, parameters, context, result):
        warnings = result["warnings"]
        t0 = time.time()
        project_path = self.parameterAsFile(parameters, "PROJECT", context)
        layout_name = self.parameterAsString(parameters, "LAYOUT", context)
        fmt = self.parameterAsString(parameters, "FORMAT", context).lower().strip()
        dpi = self.parameterAsInt(parameters, "DPI", context)
        output_base = self.parameterAsString(parameters, "OUTPUT_BASE", context)
        use_atlas = self.parameterAsBool(parameters, "ATLAS", context)
        try:
            opts = json.loads(self.parameterAsString(parameters, "OPTIONS", context) or "{}")
        except ValueError:
            opts = {}
        pages_spec = self.parameterAsString(parameters, "PAGES", context).strip()
        part = self.parameterAsInt(parameters, "PART", context) or 1
        parts = self.parameterAsInt(parameters, "PARTS", context) or 1
        paged = bool(pages_spec) or parts > 1
        profile = (self.parameterAsString(parameters, "MODE", context) or "render").strip() == "profile"

        if fmt not in RASTER_FORMATS and fmt not in ("pdf", "svg"):
            raise WorkerError("bad_format", fmt=fmt)

        self._progress(2)
        t_load = time.perf_counter()
        self._mark("stage", "stage_load")
        project = self._load_project(project_path, opts, warnings)
        self._timing["load"] = time.perf_counter() - t_load
        self._progress(25)

        layout = project.layoutManager().layoutByName(layout_name)
        if layout is None:
            raise WorkerError("layout_missing", name=layout_name)
        if not hasattr(layout, "atlas"):
            raise WorkerError("layout_is_report", name=layout_name)

        atlas = layout.atlas()
        atlas_on = bool(use_atlas and atlas is not None and atlas.enabled())
        if profile:
            result["profile"] = self._profile(project, layout, fmt, dpi, atlas_on, opts)
            return []
        t_export = time.perf_counter()
        self._mark("stage", "stage_export")

        # naming by layout name: Layout.png, several pages → Layout_1.png, Layout_2.png…
        # (atlas: Layout_<sheet no.> directly in the output folder)
        numbered = opts.get("naming") == "layout"
        out_dir = output_base if atlas_on and not numbered else os.path.dirname(output_base)
        os.makedirs(out_dir or ".", exist_ok=True)

        ext, settings = self._export_settings(fmt, dpi, opts)
        single_pdf = fmt == "pdf" and opts.get("atlas_single_pdf", True) and not paged
        self._progress(30)
        if atlas_on and (paged or numbered) and not single_pdf:
            files = self._export_atlas_part(layout, fmt, ext, settings, output_base, pages_spec, part, parts,
                                            numbered)
        elif atlas_on:
            files = self._export_atlas(layout, fmt, ext, settings, output_base, single_pdf, numbered)
        else:
            files = self._export_layout(layout, fmt, ext, settings, output_base, pages_spec, part, parts, paged)
            if numbered and fmt != "pdf" and layout.pageCollection().pageCount() > 1:
                files = self._number_first_page(output_base, files, t0)

        # only files that really exist (no scanning of the shared output folder,
        # where other processes write at the same time)
        files = [f for f in files if os.path.isfile(f)]
        self._timing["export"] = time.perf_counter() - t_export
        self._progress(100)
        return files

    # --------------------------------------------------------------- project
    def _load_project(self, path, opts, warnings):
        project = QgsProject.instance()
        flags = self._read_flags(opts)
        ok = project.read(path, flags) if flags is not None else project.read(path)
        if not ok:
            raise WorkerError("project_load", error=project.error())

        invalid = [lyr.name() for lyr in project.mapLayers().values() if not lyr.isValid()]
        if invalid:
            names = ", ".join(invalid[:15]) + (f" (+{len(invalid) - 15})" if len(invalid) > 15 else "")
            if opts.get("fail_on_invalid_layers"):
                raise WorkerError("invalid_layers", names=names)
            warnings.append({"c": "invalid_layers", "a": {"names": names}})
            self._mark("warning", "invalid_layers", names=names)
        return project

    @staticmethod
    def _read_flags(opts):
        """Flags for a fast, safe read of the project just for printing.

        - DontLoad3DViews – 3D views are not needed,
        - TrustLayerMetadata – no layer extent computation (faster for databases/WFS),
        - ForceReadOnlyLayers – sources opened read-only: no locks or writes to
          GeoPackage/SQLite when many processes read the same file,
        - DontStoreOriginalStyles – no copies of layer styles in memory.
        Flags missing in a given QGIS version are skipped.
        """
        pf = getattr(Qgis, "ProjectReadFlag", None)
        if pf is None:
            return None
        names = ["DontLoad3DViews", "ForceReadOnlyLayers", "DontStoreOriginalStyles"]
        if opts.get("trust_layer_metadata", True):
            names.append("TrustLayerMetadata")
        flags = None
        for name in names:
            flag = getattr(pf, name, None)
            if flag is not None:
                flags = flag if flags is None else flags | flag
        return flags

    # --------------------------------------------------------------- helpers
    def _atlas_feedback(self):
        fb = QgsFeedback()
        fb.progressChanged.connect(lambda v: self._progress(30 + v * 0.69))
        return fb

    @staticmethod
    def _call_iter(func, *args, feedback=None):
        """Static atlas exports – with feedback when the API accepts it."""
        if feedback is not None:
            try:
                return func(*args, feedback=feedback)
            except TypeError:
                pass
        return func(*args)

    @staticmethod
    def _collect_new(folder, since):
        """New files in a folder owned only by this job (atlas named by template)."""
        if not folder or not os.path.isdir(folder):
            return []
        out = []
        for root, _dirs, files in os.walk(folder):
            for f in files:
                p = os.path.join(root, f)
                if os.path.getmtime(p) >= since - 1:
                    out.append(p)
        return sorted(out)

    @staticmethod
    def _page_names(stem, ext, pages):
        """Names QGIS uses for pages: 1 → stem.ext, n → stem_n.ext (computed, not searched)."""
        return [stem + ext if i == 0 else f"{stem}_{i + 1}{ext}" for i in pages]

    @staticmethod
    def _check(res, err=""):
        if res != SUCCESS:
            raise _export_error(res, err)

    # ------------------------------------------------------ export settings
    @staticmethod
    def _export_settings(fmt, dpi, opts):
        """(extension, export settings) – one place for all formats."""
        if fmt in RASTER_FORMATS:
            s = QgsLayoutExporter.ImageExportSettings()
            s.generateWorldFile = bool(opts.get("world_file", False))
            s.cropToContents = bool(opts.get("crop_to_content", False))
            ext = RASTER_FORMATS[fmt]
        else:
            if fmt == "pdf":
                s = QgsLayoutExporter.PdfExportSettings()
                s.rasterizeWholeImage = False
                s.forceVectorOutput = bool(opts.get("pdf_force_vector", False))
                s.appendGeoreference = bool(opts.get("pdf_georeference", True))
                ext = ".pdf"
            else:
                s = QgsLayoutExporter.SvgExportSettings()
                ext = ".svg"
            trf = getattr(Qgis, "TextRenderFormat", None)
            if trf is not None:
                s.textRenderFormat = trf.AlwaysOutlines if opts.get("text_render") == "outlines" else trf.AlwaysText
        s.dpi = dpi
        s.exportMetadata = bool(opts.get("export_metadata", True))
        if not opts.get("antialiasing", True):
            try:
                s.flags = s.flags & ~_enum(Qgis, "LayoutRenderFlag", "Antialiasing")
            except Exception:  # noqa: BLE001
                pass
        return ext, s

    @staticmethod
    def _export_one(exporter, fmt, path, settings):
        if fmt in RASTER_FORMATS:
            return exporter.exportToImage(path, settings)
        if fmt == "pdf":
            return exporter.exportToPdf(path, settings)
        return exporter.exportToSvg(path, settings)

    # -------------------------------------------------- layout (no atlas)
    def _export_layout(self, layout, fmt, ext, settings, base, pages_spec, part, parts, paged):
        if fmt == "pdf":  # the whole document in one file
            self._check(QgsLayoutExporter(layout).exportToPdf(base + ext, settings))
            return [base + ext]
        total = layout.pageCollection().pageCount()
        pages = list(range(total))
        if paged and fmt in RASTER_FORMATS:
            pages = split_part(parse_pages(pages_spec, total), part, parts)
            self._mark("info", "pages_info", pages=", ".join(str(i + 1) for i in pages) or "–", total=total)
            if not pages:
                return []
            settings.pages = pages
        self._check(self._export_one(QgsLayoutExporter(layout), fmt, base + ext, settings))
        return self._page_names(base, ext, pages)

    @staticmethod
    def _number_first_page(base, files, since):
        """Several pages: QGIS writes base.png, base_2.png… – the first becomes base_1.png.

        Only files created by this export are renamed: the page itself and its
        side files (world file, .aux.xml) – never e.g. a project file with the same name.
        """
        candidates = [f for f in files if os.path.splitext(f)[0] == base]
        for f in list(candidates):
            candidates.append(f + ".aux.xml")
            candidates += [base + side for side in WORLD_EXT.get(os.path.splitext(f)[1].lower(), ()) + (".wld",)]
        renamed = {}
        for src in candidates:
            if os.path.isfile(src) and os.path.getmtime(src) >= since - 1:
                dst = base + "_1" + src[len(base):]
                os.replace(src, dst)
                renamed[src] = dst
        return [renamed.get(f, f) for f in files]

    # ----------------------------------------------------------- atlas
    def _export_atlas(self, layout, fmt, ext, settings, base, single_pdf, numbered):
        """Whole atlas with the built-in QGIS iterator (names from the atlas expression)."""
        atlas, fb, t0 = layout.atlas(), self._atlas_feedback(), time.time()
        if single_pdf:
            if numbered:
                target = base + ext
            else:
                os.makedirs(base, exist_ok=True)
                target = os.path.join(base, os.path.basename(base) + ext)
            res, err = self._call_iter(QgsLayoutExporter.exportToPdf, atlas, target, settings, feedback=fb)
            self._check(res, err)
            return [target]
        os.makedirs(base, exist_ok=True)
        prefix = os.path.join(base, "atlas")
        if fmt in RASTER_FORMATS:
            res, err = self._call_iter(QgsLayoutExporter.exportToImage, atlas, prefix, ext.lstrip("."), settings,
                                       feedback=fb)
        elif fmt == "pdf":
            res, err = self._call_iter(QgsLayoutExporter.exportToPdfs, atlas, prefix, settings, feedback=fb)
        else:
            res, err = self._call_iter(QgsLayoutExporter.exportToSvg, atlas, prefix, settings, feedback=fb)
        self._check(res, err)
        return self._collect_new(base, t0)

    def _export_atlas_part(self, layout, fmt, ext, settings, base, pages_spec, part, parts, numbered):
        """Part of an atlas: selected sheets, each as a separate file.

        numbered=True: files base_<sheet no.> side by side in the output folder,
        otherwise: names from the atlas expression in the `base` subfolder.
        """
        atlas = layout.atlas()
        if not atlas.beginRender():
            raise WorkerError("atlas_prepare")
        try:
            total = atlas.count()
            mine = split_part(parse_pages(pages_spec, total), part, parts)
            if mine:
                self._mark("info", "sheets_info", first=mine[0] + 1, last=mine[-1] + 1, count=len(mine), total=total)
            folder = os.path.dirname(base) if numbered else base
            os.makedirs(folder, exist_ok=True)
            page_range = [0] if fmt == "pdf" else list(range(layout.pageCollection().pageCount()))
            exporter = QgsLayoutExporter(layout)  # one exporter for all sheets
            stem_name = os.path.basename(base)
            files = []
            for n, idx in enumerate(mine):
                if self._feedback.isCanceled():
                    break
                if not atlas.seekTo(idx):
                    self._mark("warning", "sheet_skipped", n=idx + 1)
                    continue
                if numbered:
                    name = f"{stem_name}_{idx + 1}"
                else:
                    name = (atlas.currentFilename() or "").strip() or f"atlas_{idx + 1}"
                    name = name.replace("/", "_").replace("\\", "_")
                stem = os.path.join(folder, name)
                self._check(self._export_one(exporter, fmt, stem + ext, settings))
                files += self._page_names(stem, ext, page_range)
                self._progress(30 + 69 * (n + 1) / len(mine))
            return files
        finally:
            atlas.endRender()

    # ============================================================ profiling
    def _profile(self, project, layout, fmt, dpi, atlas_on, opts):
        self._mark("stage", "stage_profile")
        """Measures where the time goes. Every measurement is a separate render, so the
        numbers are approximate, but they show clearly which part is the slowest."""
        prof = {"load": self._timing.get("load", 0.0), "labels": 0.0, "page": 0.0, "other": 0.0, "save": 0.0,
                "maps": 0, "pages": layout.pageCollection().pageCount(), "atlas_sheet": 0,
                "total_mem": total_memory(), "layers": {},
                "invalid": [lyr.name() for lyr in project.mapLayers().values() if not lyr.isValid()]}
        atlas = layout.atlas()
        if atlas_on:
            if not atlas.beginRender() or atlas.count() == 0:
                raise WorkerError("atlas_prepare")
            atlas.seekTo(0)  # the first sheet is representative for the whole atlas
            prof["atlas_sheet"] = 1
        try:
            maps = [item for item in layout.items() if isinstance(item, QgsLayoutItemMap)]
            maps_full = 0.0
            for i, item in enumerate(maps):
                ms = self._item_map_settings(layout, item, dpi)
                if ms is None:
                    continue
                prof["maps"] += 1
                full = self._render_time(ms)
                no_labels = QgsMapSettings(ms)
                self._set_labels(no_labels, False)
                without = self._render_time(no_labels)
                prof["labels"] += max(0.0, full - without)
                maps_full += full
                layers = [lyr for lyr in ms.layers() if lyr is not None]
                for j, layer in enumerate(layers):
                    self._profile_layer(project, ms, no_labels, layer, prof["layers"])
                    self._progress(30 + 55 * (i + (j + 1) / max(1, len(layers))) / max(1, len(maps)))
            self._progress(86)

            exporter = QgsLayoutExporter(layout)
            t = time.perf_counter()
            images = [exporter.renderPageToImage(p, QSize(), dpi) for p in range(prof["pages"])]
            prof["page"] = time.perf_counter() - t
            prof["other"] = max(0.0, prof["page"] - maps_full)
            self._progress(93)
            prof["save"] = self._save_time(layout, fmt, dpi, opts, images, prof["page"])
        finally:
            if atlas_on:
                atlas.endRender()
        prof["layers"] = sorted(prof["layers"].values(), key=lambda e: e["render"], reverse=True)
        prof["peak_mem"] = peak_memory()
        self._progress(100)
        return prof

    @staticmethod
    def _item_map_settings(layout, item, dpi):
        """Map settings of a layout map at the export resolution (size in pixels)."""
        try:
            rect = item.rect()
            try:
                mm = _enum(Qgis, "LayoutUnit", "Millimeters")
                conv = layout.renderContext().measurementConverter()
                w = conv.convert(QgsLayoutMeasurement(rect.width(), layout.units()), mm).length()
                h = conv.convert(QgsLayoutMeasurement(rect.height(), layout.units()), mm).length()
            except Exception:  # noqa: BLE001 – layouts are in millimetres by default
                w, h = rect.width(), rect.height()
            size = QSizeF(max(1.0, w / 25.4 * dpi), max(1.0, h / 25.4 * dpi))
            return item.mapSettings(item.extent(), size, dpi, True)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _set_labels(ms, on):
        flag = getattr(getattr(Qgis, "MapSettingsFlag", None), "DrawLabeling", None)
        if flag is None:
            flag = QgsMapSettings.DrawLabeling
        ms.setFlag(flag, on)

    @staticmethod
    def _render_time(ms):
        job = QgsMapRendererSequentialJob(ms)
        t = time.perf_counter()
        job.start()
        job.waitForFinished()
        return time.perf_counter() - t

    def _profile_layer(self, project, ms, no_labels, layer, layers):
        layer_type = layer.type()
        vector, raster = layer_type == VECTOR_LAYER, layer_type == RASTER_LAYER
        entry = layers.setdefault(layer.id(), {
            "name": layer.name(), "provider": layer.providerType(),
            "kind": "vector" if vector else "raster" if raster else "other",
            "remote": self._is_remote(layer), "render": 0.0, "fetch": 0.0, "features": 0, "hidden": False,
        })
        single = QgsMapSettings(no_labels)
        single.setLayers([layer])
        entry["render"] += self._render_time(single)  # data + drawing (cold)
        if not vector:
            return
        if layer.hasScaleBasedVisibility() and not layer.isInScaleRange(ms.scale()):
            entry["hidden"] = True  # not drawn at this scale – no data is read
            return
        request = QgsFeatureRequest()
        try:
            ct = QgsCoordinateTransform(ms.destinationCrs(), layer.crs(), project.transformContext())
            request.setFilterRect(ct.transformBoundingBox(ms.visibleExtent()))
        except Exception:  # noqa: BLE001
            pass
        t, n = time.perf_counter(), 0
        for _feature in layer.getFeatures(request):
            n += 1
        entry["fetch"] += time.perf_counter() - t  # data only (warm connection)
        entry["features"] += n

    @staticmethod
    def _is_remote(layer):
        remote = {"postgres", "wfs", "oapif", "oracle", "mssql", "hana", "arcgisfeatureserver", "wms", "wcs",
                  "arcgismapserver", "xyz", "vectortile"}
        source = layer.source().lower()
        return layer.providerType() in remote or source.startswith(("http://", "https://", "/vsicurl")) or \
            "url=" in source

    def _save_time(self, layout, fmt, dpi, opts, images, page_time):
        """Time of writing the result: encoding the image (raster) or the whole PDF/SVG export minus drawing."""
        tmp = tempfile.mkdtemp(prefix="multirender_profile_")
        try:
            if fmt in RASTER_FORMATS:
                t = time.perf_counter()
                for i, image in enumerate(images):
                    image.save(os.path.join(tmp, f"p{i}{RASTER_FORMATS[fmt]}"))
                return time.perf_counter() - t
            ext, settings = self._export_settings(fmt, dpi, opts)
            t = time.perf_counter()
            self._export_one(QgsLayoutExporter(layout), fmt, os.path.join(tmp, "p" + ext), settings)
            return max(0.0, time.perf_counter() - t - page_time)
        finally:
            for f in os.listdir(tmp):
                try:
                    os.remove(os.path.join(tmp, f))
                except OSError:
                    pass
            try:
                os.rmdir(tmp)
            except OSError:
                pass
