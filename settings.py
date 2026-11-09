"""Plugin settings: defaults for new layouts, execution and export options."""

import json
import os

from qgis.core import QgsSettings

PREFIX = "MultiRender/"

# (format key, label, file extension)
FORMATS = [
    ("png", "PNG", ".png"),
    ("jpg", "JPG", ".jpg"),
    ("tif", "TIFF / GeoTIFF", ".tif"),
    ("bmp", "BMP", ".bmp"),
    ("pdf", "PDF", ".pdf"),
    ("pdf_raster", "PDF (PNG)", ".pdf"),  # the whole page as one lossless image – nothing to select
    ("svg", "SVG", ".svg"),
]
FORMAT_EXT = {f[0]: f[2] for f in FORMATS}
FORMAT_LABEL = {f[0]: f[1] for f in FORMATS}
PDF_FORMATS = ("pdf", "pdf_raster")


MAX_PARALLEL = 10  # upper limit of processes running at the same time (and of parts per layout)


# size of the main window for screens from Full HD to 4K (logical pixels, before clamping to the screen)
WINDOW_SIZES = {
    "1080p": (1400, 800),
    "1440p": (1900, 1080),
    "2160p": (2800, 1600),
}
AUTO_SCREEN_SHARE = 0.8  # "auto": this share of the free screen area


def window_size(available_width, available_height, mode=None):
    """(width, height) of the main window for the chosen mode, never larger than 95% of the screen."""
    mode = mode or get("window_size")
    if mode in WINDOW_SIZES:
        w, h = WINDOW_SIZES[mode]
    else:
        w, h = available_width * AUTO_SCREEN_SHARE, available_height * AUTO_SCREEN_SHARE
    w = min(w, available_width * 0.95)
    h = min(h, available_height * 0.95)
    return max(900, int(w)), max(560, int(h))


def default_parallel():
    cpus = os.cpu_count() or 2
    return max(1, min(6, cpus - 1))


DEFAULTS = {
    # general
    "language": "en",
    "window_size": "auto",  # "auto" | "1080p" | "1440p" | "2160p"
    "max_parallel": default_parallel(),
    "output_dir": os.path.join(os.path.expanduser("~"), "QGIS_render"),
    "remember": True,  # remember the settings of each layout
    # defaults for layouts without saved settings (new projects)
    "format": "png",
    "dpi": 300,
    "use_atlas": True,
    "parts": 1,  # processes per layout
    "defaults_follow_last": True,  # values chosen in the table become the new defaults
    "open_new_layout": True,  # “Add layout” opens the new layout in the layout designer
    # file names
    "naming": "layout",  # "layout" (Layout, Layout_1…) | "template"
    "filename_template": "{project}_{layout}",
    "overwrite": True,
    # advanced
    "qgis_process": "",
    "trust_layer_metadata": True,
    "fail_on_invalid_layers": False,
    # raster
    "world_file": False,
    "crop_to_content": False,
    "antialiasing": True,
    # PDF / SVG
    "pdf_georeference": True,
    "pdf_force_vector": False,
    "text_render": "text",  # "text" | "outlines"
    "atlas_single_pdf": True,
    "export_metadata": True,
    # project cache
    "cache_mode": "remote",  # "off" | "remote" | "all_vector"
    "cache_extent_filter": True,
    "cache_reuse": False,
    "cache_skip_unused": True,  # remote layers not used by the printed layouts → empty local copies
    # memory of layout rows (JSON: "project::layout" -> row settings)
    "row_memory": "{}",
}

_qs = None


def _settings():
    """One shared QgsSettings instance instead of a new one on every read."""
    global _qs
    if _qs is None:
        _qs = QgsSettings()
    return _qs


def get(key):
    default = DEFAULTS[key]
    value = _settings().value(PREFIX + key, default)
    # QgsSettings may return strings – cast to the type of the default value
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes")
        return bool(value)
    if isinstance(default, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        if key in ("max_parallel", "parts"):  # older saved values may be higher than the limit
            value = max(1, min(MAX_PARALLEL, value))
        return value
    return "" if value is None else str(value)


def set(key, value):  # noqa: A001 (module-level setter)
    _settings().setValue(PREFIX + key, value)


def naming_mode():
    return "template" if get("naming") == "template" else "layout"


def load_row_memory():
    try:
        data = json.loads(get("row_memory") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def save_row_memory(data, limit=1000):
    if len(data) > limit:  # the oldest entries are dropped
        data = dict(list(data.items())[-limit:])
    set("row_memory", json.dumps(data, ensure_ascii=False))
    return data


def cache_options():
    return {
        "cache_mode": get("cache_mode"),
        "cache_extent_filter": get("cache_extent_filter"),
        "cache_skip_unused": get("cache_skip_unused"),
    }


EXPORT_KEYS = (
    "trust_layer_metadata",
    "fail_on_invalid_layers",
    "world_file",
    "crop_to_content",
    "antialiasing",
    "pdf_georeference",
    "pdf_force_vector",
    "text_render",
    "atlas_single_pdf",
    "export_metadata",
)


def export_options():
    """Options passed to the rendering process (JSON)."""
    options = {k: get(k) for k in EXPORT_KEYS}
    options["naming"] = naming_mode()
    return options
