"""Analysis of performance measurements: shares of stages and layers, practical tips.

Pure Python (no Qt) – used by the report window and by the summary after a normal run.
"""

from .i18n import num, tr

# thresholds (share of the measured time) above which a tip is shown
LOAD_SHARE = 0.30
LABELS_SHARE = 0.25
OTHER_SHARE = 0.20
SAVE_SHARE = 0.20
LAYER_SHARE = 0.15
FETCH_RATIO = 0.5  # data fetch / layer time
MEMORY_RATIO = 0.8  # (peak memory × processes) / physical memory


def mb(value):
    return f"{num(value / 1024 ** 2, 0)} MB" if value else "?"


def secs(value):
    return num(value, 2) + " s"


def pct(share):
    return f"{share * 100:.0f}"


def analyze(profile, parallel, cache_on):
    """Returns {"total", "stages": [(label, seconds, share)], "layers": [...], "tips": [str]}."""
    layers = []
    for entry in profile.get("layers", []):
        render = float(entry.get("render", 0.0))
        fetch = min(float(entry.get("fetch", 0.0)), render) if entry.get("kind") == "vector" else 0.0
        layers.append({**entry, "render": render, "fetch": fetch, "draw": max(0.0, render - fetch)})
    layers_total = sum(e["render"] for e in layers)

    stages = [
        (tr("Project loading"), float(profile.get("load", 0.0))),
        (tr("Layers (data + drawing)"), layers_total),
        (tr("Labels"), float(profile.get("labels", 0.0))),
        (tr("Other layout items (legend, tables, …)"), float(profile.get("other", 0.0))),
        (tr("Writing the file"), float(profile.get("save", 0.0))),
    ]
    total = sum(t for _, t in stages) or 1e-9
    share = {label: t / total for label, t in stages}
    for e in layers:
        e["share"] = e["render"] / total

    tips = []
    invalid = profile.get("invalid") or []
    if invalid:
        names = ", ".join(invalid[:8]) + (f" (+{len(invalid) - 8})" if len(invalid) > 8 else "")
        tips.append(tr("{n} layers are unavailable in the rendering process ({names}). They are not printed, "
                       "and waiting for their connections slows down project loading. They usually need a login "
                       "kept only in the QGIS session; with the project cache on, vector layers of the current "
                       "project are copied from the session.", n=len(invalid), names=names))
    load_share = share[stages[0][0]]
    if load_share >= LOAD_SHARE:
        tips.append(tr("Project loading takes {pct}% of the time and every process loads the project again. "
                       "Remove unused layers, keep “Fast project loading” on and avoid splitting small layouts "
                       "into many processes.", pct=pct(load_share)))
    labels_share = share[stages[2][0]]
    if labels_share >= LABELS_SHARE:
        tips.append(tr("Labels take {pct}% of the time. Label fewer layers, use scale-dependent labelling, "
                       "simpler placement or fewer candidate positions.", pct=pct(labels_share)))
    other_share = share[stages[3][0]]
    if other_share >= OTHER_SHARE:
        tips.append(tr("Layout items other than maps (legend, attribute tables, HTML frames, pictures) take "
                       "{pct}% of the time. Check large tables, legends filtered by map content and big "
                       "embedded pictures.", pct=pct(other_share)))
    save_share = share[stages[4][0]]
    if save_share >= SAVE_SHARE:
        tips.append(tr("Writing the file takes {pct}% of the time. At high DPI PNG compression is slow – "
                       "consider TIFF or JPG, a lower DPI or a faster disk.", pct=pct(save_share)))

    for e in layers[:3]:
        if e["share"] < LAYER_SHARE:
            break
        name, layer_pct = e["name"], pct(e["share"])
        if e["kind"] == "vector" and e["render"] and e["fetch"] / e["render"] >= FETCH_RATIO:
            if e.get("remote"):
                tip = tr("Layer “{name}” ({pct}%): most of its time is reading data from {provider}. "
                         "The project cache fetches it once per run; on the server a spatial index and "
                         "a smaller set of columns help.", name=name, pct=layer_pct, provider=e["provider"]) \
                    if cache_on else \
                    tr("Layer “{name}” ({pct}%): most of its time is reading data from {provider}. Turn on "
                       "the project cache so that it is fetched only once per run.",
                       name=name, pct=layer_pct, provider=e["provider"])
            else:
                tip = tr("Layer “{name}” ({pct}%): most of its time is reading the file. Use a format with "
                         "a spatial index (GeoPackage, FlatGeobuf) or create a spatial index.",
                         name=name, pct=layer_pct)
        elif e["kind"] == "raster":
            tip = tr("Raster “{name}” ({pct}%): build overviews (pyramids) or use a Cloud Optimized GeoTIFF; "
                     "avoid on-the-fly reprojection of large rasters.", name=name, pct=layer_pct)
        else:
            tip = tr("Layer “{name}” ({pct}%): drawing takes {secs} for {n} features. Simplify the symbology "
                     "(draw effects, many symbol layers, complex markers), use scale-dependent visibility or "
                     "geometry simplification.", name=name, pct=layer_pct, secs=secs(e["draw"]),
                     n=e.get("features", 0))
        tips.append(tip)

    peak, total_mem = int(profile.get("peak_mem", 0)), int(profile.get("total_mem", 0))
    if peak and total_mem and peak * parallel > MEMORY_RATIO * total_mem:
        tips.append(tr("One process needs about {mem}, so {n} parallel processes need about {all} of {total} "
                       "of memory. Lower the number of parallel processes to avoid swapping.",
                       mem=mb(peak), n=parallel, all=mb(peak * parallel), total=mb(total_mem)))
    if not tips:
        tips.append(tr("No single bottleneck – the time is spread over several stages."))

    return {"total": total, "stages": [(label, t, share[label]) for label, t in stages], "layers": layers,
            "tips": tips}


def report_text(name, profile, analysis, fmt, dpi):
    """Plain-text version of the report (for copying)."""
    lines = [tr("Performance of “{name}” – {fmt}, {dpi} dpi", name=name, fmt=fmt.upper(), dpi=dpi)]
    if profile.get("atlas_sheet"):
        lines.append(tr("Measured on the first atlas sheet."))
    if profile.get("cache_seconds"):
        lines.append(tr("Measured with the project cache (built in {secs}, once per run).",
                        secs=secs(profile["cache_seconds"])))
    lines.append(tr("Measured time: {secs} · peak memory of the process: {mem}",
                    secs=secs(analysis["total"]), mem=mb(profile.get("peak_mem", 0))))
    lines.append("")
    for label, t, share in analysis["stages"]:
        lines.append(f"{label:<45} {secs(t):>10} {pct(share):>4}%")
    lines.append("")
    for e in analysis["layers"]:
        lines.append(f"{e['name'][:40]:<40} {e['provider']:<10} {secs(e['render']):>10} {pct(e['share']):>4}%  "
                     + tr("data {fetch} · drawing {draw} · {n} features",
                          fetch=secs(e["fetch"]), draw=secs(e["draw"]), n=e.get("features", 0)))
    lines.append("")
    lines += [f"• {tip}" for tip in analysis["tips"]]
    return "\n".join(lines)


def run_summary(results, parts_used):
    """Summary of a normal run from the timings of the processes; returns (text, tip or None)."""
    load = sum(float(r.get("timing", {}).get("load", 0.0)) for r in results)
    export = sum(float(r.get("timing", {}).get("export", 0.0)) for r in results)
    peak = max((int(r.get("peak_mem", 0)) for r in results), default=0)
    if load + export <= 0:
        return None, None
    load_share = load / (load + export)
    text = tr("Time in the processes: project loading {load}%, rendering {render}% · peak memory per "
              "process: {mem}", load=pct(load_share), render=pct(1 - load_share), mem=mb(peak))
    tip = None
    if load_share >= 0.4:
        tip = tr("Loading the project takes a large part of the time – fewer processes per layout may be "
                 "faster.") if parts_used else \
            tr("Loading the project takes a large part of the time – remove unused layers or keep "
               "“Fast project loading” on.")
    return text, tip
