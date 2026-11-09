"""Short summary of a run from the timings reported by the rendering processes.

Pure Python (no Qt): shares of project loading and rendering, peak memory, one tip.
"""

from .i18n import num, tr


def mb(value):
    return f"{num(value / 1024 ** 2, 0)} MB" if value else "?"


def pct(share):
    return f"{share * 100:.0f}"


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
