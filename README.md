# MultiRender – parallel rendering of QGIS print layouts

QGIS **3.40+** plugin (also prepared for QGIS 4 / Qt6). It exports print layouts and atlases in
parallel – every layout (or part of a layout) runs in its own `qgis_process` process, so all CPU
cores are used and QGIS stays responsive.

Interface languages: **English** (default), Polish, German, Spanish, French – *Settings → General → Language*.

## Installation

1. QGIS → *Plugins → Manage and Install Plugins → Install from ZIP* → choose `multirender.zip`.
2. The toolbar icon and *Plugins → MultiRender* appear.

## Usage

1. Open a project and start the plugin. The table lists **all layouts from the Layout Manager**,
   one row per layout. The list follows the Layout Manager automatically; **Refresh** forces a reload.
2. For every layout choose the **format, DPI, atlas, pages, processes and output folder**.
   Layouts without saved settings get the values from *Settings → New projects*.
3. **Duplicate** a row to get the same map in another format; bulk edit applies format/DPI/processes/folder
   to the selected rows.
4. **Add layout** creates a new, empty layout, **Copy layout** copies one (`name (1)`, `name (2)`…),
   **Editor** (or double-click the name) opens the layout designer, **Remove** deletes the ticked layouts.
5. Click **Render**. While working the window is locked (only **Stop** is active), a clock shows the time of
   the whole run and at the end a message says “Printed N prints in X s”.
6. The **Prints** panel collects the results: open, show folder, **Pack to ZIP**.
7. **Other projects** adds layouts from other `.qgs/.qgz` files (its arrow: from a whole folder);
   lists can be saved and loaded as JSON.

Toolbar: Add layout · Copy layout · Duplicate · Select all · Select none · Editor · Remove · Refresh ·
Other projects · Load list · Save list · Settings.

## Formats

PNG, JPG, TIFF/GeoTIFF, BMP, PDF, **PDF (PNG)** and SVG. *PDF (PNG)* is a PDF in which every page is one
lossless image at the chosen DPI – lines, polygons and text cannot be selected or edited in a PDF editor.

## Settings

| Tab | Contents |
|---|---|
| General | language, window size (fit to the screen, or for Full HD / QHD / 4K screens) |
| New projects | default format, DPI, atlas, processes per layout, “update defaults with the last chosen values”, file names (layout name or template), overwriting |
| Rendering | render folder and remembering the settings of each layout; export (world file, crop, antialiasing, GeoPDF, vector output, single-PDF atlas, text as text/outlines, metadata, fast project loading, stop on missing layers); project cache (what to cache, extent filter, unused layers, reuse) |
| Advanced | simultaneous renders (up to 10), `qgis_process` path (detect / test) |

## File names

Default: **layout name** without the QGIS file name – one page → `al-jerozolimskie.png`, several pages →
`al-jerozolimskie_1.png`, `al-jerozolimskie_2.png`…, atlas → `al-jerozolimskie_<sheet no.>`.
The same name from several projects → `al-jerozolimskie (2)`. Alternatively a template with
`{project}`, `{layout}`, `{format}`, `{dpi}`, `{date}`, `{time}`, `{index}`.

## Parallel pages and atlas sheets

- **Pages** – range to print, e.g. `2-5`, `1,3,7-9`, `10-` (empty = all); for an atlas these are sheet numbers.
- **Processes** – into how many parallel processes to split the layout (atlas 120 sheets × 4 →
  1–30, 31–60, 61–90, 91–120 at the same time). Works for atlases (all formats) and multi-page
  PNG/JPG/TIFF/BMP; PDF/SVG without an atlas is exported as a whole.

## Run summary

After every run the log shows how the time in the processes was split between project loading and
rendering, and the peak memory per process.

## Project cache

Before rendering, remote and database vector layers (PostGIS, WFS, OGC API, Oracle, MSSQL, files over HTTP)
**used by the printed layouts** are copied once into a local GeoPackage on disk (system temporary folder),
optionally only within the extent of the layout maps. All parallel prints read from this copy.

- **Current project – built inside the QGIS session.** The layers are copied in a background task from the
  layers you are already connected to, so logins that exist only in the session (master password, passwords
  typed when connecting) are not needed by the rendering processes. A separate project object writes the
  copy; the open project is not modified.
- **Other projects** – built by a `qgis_process` worker.
- Layers referenced by expressions (`aggregate`, `get_feature`, …) are copied without the extent filter;
  layers joined to used layers are copied as well.
- Optionally, remote layers **not used** by the printed layouts are replaced by empty local copies, so the
  rendering processes do not wait for their connections (*Settings → Rendering → Project cache*).
- The cache is removed after the run (unless reuse is enabled); local layers and WMS/XYZ basemaps are not copied.
- If layers are unavailable in the rendering processes, the log and the final message say so – such prints may be missing data.

## How it works

```
plugin window ── [1 × qgis_process run worker/prepare_cache.py]  → cache: GeoPackage + project copy
              └─ RenderManager (QProcess queue, N at a time)
                   ├─ qgis_process run worker/render_layout.py --PROJECT=… --LAYOUT=… --FORMAT=… --DPI=…
                   └─ …
```

Workers report progress and messages as `@@MR:` lines with message codes; the plugin translates them
into the selected language. Translations live in `i18n/*.py` (keys are the English source texts).

## Troubleshooting

- *qgis_process not found* – set it in *Settings → Advanced*: Windows `…\apps\qgis-ltr\bin\qgis_process.exe`
  (or `…\bin\qgis_process-qgis-ltr.bat`), Linux `/usr/bin/qgis_process`,
  macOS `/Applications/QGIS-LTR.app/Contents/MacOS/bin/qgis_process`.
- Error details: hover over the row status or see the log at the bottom of the window.

## Links

- Homepage: https://github.com/DaWhere/MultiRender
- Bug reports and feature requests: https://github.com/DaWhere/MultiRender
- Source code: https://github.com/DaWhere/MultiRender
- Author: Dawid Matusiak – [LinkedIn](https://www.linkedin.com/in/dawid-matusiak-b1666421b)
