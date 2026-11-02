"""Interface translations for MultiRender.

Source strings in the code are English. Other languages live in small Python
dictionaries (pl.py, de.py, es.py, fr.py) keyed by the English text, so no
compiled Qt .qm files are needed.

Messages from the worker processes (qgis_process) are sent as codes with
arguments and translated here, in the plugin, in the selected language.
"""

import importlib
import sys

LANGUAGES = [
    ("en", "English"),
    ("pl", "Polski"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("fr", "Français"),
]
CODES = {code for code, _ in LANGUAGES}
_DECIMAL_COMMA = {"pl", "de", "es", "fr"}

_lang = "en"
_table = {}


def set_language(code):
    """Switch the active language (unknown codes fall back to English)."""
    global _lang, _table
    code = code if code in CODES else "en"
    _lang = code
    if code == "en":
        _table = {}
        return
    # always read the file again: after updating the plugin without restarting QGIS an old
    # version of the module could still be cached (QGIS does not unload modules imported this way)
    name = f"{__name__}.{code}"
    module = sys.modules.get(name)
    _table = (importlib.reload(module) if module is not None else importlib.import_module(name)).T


def language():
    return _lang


def tr(text, /, **kwargs):
    """Translated text; named placeholders are filled only when arguments are given."""
    translated = _table.get(text, text)
    if not kwargs:
        return translated
    try:
        return translated.format(**kwargs)
    except (KeyError, IndexError, ValueError):  # broken translation – fall back to English
        return text.format(**kwargs)


def num(value, digits=1):
    """Number with the decimal separator of the active language."""
    text = f"{value:.{digits}f}"
    return text.replace(".", ",") if _lang in _DECIMAL_COMMA else text


# Messages reported by the worker processes: code -> English template.
MESSAGES = {
    "raw": "{text}",
    "bad_format": "Unsupported format: {fmt}",
    "project_load": "Cannot load the project: {error}",
    "layout_missing": "Layout “{name}” not found in the project",
    "layout_is_report": "“{name}” is a report, not a print layout",
    "invalid_layers": "Unavailable layers: {names}",
    "atlas_prepare": "Cannot prepare the atlas (no features in the coverage layer?)",
    "sheet_skipped": "Skipped atlas sheet {n}",
    "pages_info": "Pages {pages} of {total}",
    "sheets_info": "Atlas sheets {first}–{last} ({count} of {total})",
    "export_canceled": "Export canceled",
    "export_memory": "Out of memory – try a lower DPI",
    "export_file": "Cannot write the file{detail}",
    "export_print": "Printing error{detail}",
    "export_svg": "SVG layer error{detail}",
    "export_iterator": "Atlas iteration error{detail}",
    "export_code": "Export error (code {code}){detail}",
    "cache_dynamic": "Layouts choose layers dynamically – caching all matching layers",
    "cache_layer": "Cached: {name} – {count} features",
    "cache_write_error": "{name}: write error ({detail}) – using the original source",
    "cache_open_error": "{name}: cannot open the local copy – using the original source",
    "cache_save_error": "Cannot save the project copy: {error}",
    # steps reported while working – used when a process ends without a result
    "stage_load": "loading the project",
    "stage_layouts": "analysing the layouts",
    "stage_copy": "copying layer “{name}”",
    "stage_save": "saving the project copy",
    "stage_export": "exporting",
    "stage_profile": "measuring",
}


def message(payload):
    """Text of a worker message: {"c": code, "a": {args}} or a plain string."""
    if isinstance(payload, dict):
        args = payload.get("a") or {}
        template = MESSAGES.get(payload.get("c"))
        if template is None:
            return str(args.get("text") or payload.get("c") or "")
        return tr(template, **args)
    return str(payload or "")
