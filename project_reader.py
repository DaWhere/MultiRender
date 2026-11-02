"""Reading the list of print layouts directly from a .qgs / .qgz file.

The project is not loaded into QGIS (that would be slow and would replace the
current project) – scanning the XML is enough.
"""

import os
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass


@dataclass
class LayoutInfo:
    name: str
    atlas: bool = False
    dpi: int = 0  # resolution saved in the layout (0 = none)
    pages: int = 0  # number of pages (0 = unknown)
    features: int = -1  # number of atlas sheets (-1 = unknown)


PROJECT_EXTENSIONS = (".qgs", ".qgz")


def is_project_file(path):
    return path.lower().endswith(PROJECT_EXTENSIONS) and os.path.isfile(path)


def _open_qgs_stream(path):
    if path.lower().endswith(".qgz"):
        zf = zipfile.ZipFile(path)
        members = [n for n in zf.namelist() if n.lower().endswith(".qgs")]
        if not members:
            zf.close()
            raise ValueError("The .qgz archive contains no .qgs file")
        return zf, zf.open(members[0])
    return None, open(path, "rb")


def read_layouts(path):
    """List of LayoutInfo for the print layouts of the project."""
    zf, stream = _open_qgs_stream(path)
    layouts = []
    try:
        in_layouts = False
        current = None
        nested = 0  # nested <Layout> elements inside a layout
        pc_depth = -1  # depth inside <PageCollection> (-1 = outside)
        # iterparse – memory-friendly for large projects
        for event, el in ET.iterparse(stream, events=("start", "end")):
            tag = el.tag
            if current is not None and pc_depth >= 0:
                if event == "start":
                    pc_depth += 1
                    if pc_depth == 1 and tag == "LayoutItem":
                        current.pages += 1
                    continue
                if tag == "PageCollection" and pc_depth == 0:
                    pc_depth = -1
                else:
                    pc_depth -= 1
                    continue
            if event == "start":
                if current is not None and nested == 0 and tag == "PageCollection":
                    pc_depth = 0
                elif tag == "Layouts" and current is None:
                    in_layouts = True
                elif in_layouts and tag == "Layout":
                    if current is None:
                        try:
                            dpi = int(float(el.get("printResolution", "0") or 0))
                        except ValueError:
                            dpi = 0
                        current = LayoutInfo(name=el.get("name", ""), dpi=dpi)
                    else:
                        nested += 1
                elif current is not None and tag == "Atlas" and nested == 0:
                    current.atlas = el.get("enabled", "0") in ("1", "true")
            elif tag == "Layout" and current is not None:
                if nested:
                    nested -= 1
                else:
                    if current.name:
                        layouts.append(current)
                    current = None
                    el.clear()
            elif tag == "Layouts" and in_layouts and current is None:
                break  # nothing more is needed
    finally:
        stream.close()
        if zf is not None:
            zf.close()
    return layouts


def find_projects(folder, recursive=True):
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(files):
            if f.lower().endswith(PROJECT_EXTENSIONS):
                found.append(os.path.join(root, f))
        if not recursive:
            break
    return sorted(found)
