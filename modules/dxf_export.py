# ============================================================
# modules/dxf_export.py
# DXF CAD Export Engine  v1.0
# ALKF Master Land Plan API
#
# Converts the site intelligence JSON dataset into a DXF file
# compatible with AutoCAD, Rhino, QGIS, and SketchUp.
#
# Layers produced:
#   SITE_BOUNDARY  — densified boundary polyline
#   VIEW_POINTS    — point entities with view label as XDATA
#   NOISE_POINTS   — point entities with dBA value as XDATA
#   NON_BUILDING   — closed polylines for non-buildable zones
# ============================================================

from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

import ezdxf
from ezdxf.enums import TextEntityAlignment

log = logging.getLogger(__name__)

# ── Layer definitions ─────────────────────────────────────────
_LAYERS = {
    "SITE_BOUNDARY": {"color": 7,   "linetype": "CONTINUOUS"},  # white/black
    "VIEW_POINTS":   {"color": 3,   "linetype": "CONTINUOUS"},  # green
    "NOISE_POINTS":  {"color": 1,   "linetype": "CONTINUOUS"},  # red
    "NON_BUILDING":  {"color": 5,   "linetype": "DASHED"},      # blue
    "LABELS":        {"color": 2,   "linetype": "CONTINUOUS"},  # yellow
}

# ── View type → DXF colour index ─────────────────────────────
_VIEW_COLOR = {
    "SEA":        4,   # cyan
    "HARBOR":     4,
    "RESERVOIR":  4,
    "MOUNTAIN":   8,   # grey
    "PARK":       3,   # green
    "GREEN":      3,
    "CITY":       1,   # red
    "OPEN":       2,   # yellow
}


def _setup_layers(doc: ezdxf.document.Drawing) -> None:
    """Register all required layers in the DXF document."""
    lt = doc.linetypes
    # Ensure DASHED linetype exists
    if "DASHED" not in lt:
        lt.add("DASHED", pattern=[0.5, 0.25, -0.25])

    layers = doc.layers
    for name, props in _LAYERS.items():
        if name not in layers:
            layer = layers.add(name)
            layer.color = props["color"]
            layer.linetype = props["linetype"]


def _write_site_boundary(
    msp,
    xs: list[float],
    ys: list[float],
) -> None:
    """Write the densified site boundary as a closed LWPOLYLINE."""
    if not xs:
        return

    pts = list(zip(xs, ys))
    # Close the polyline back to origin
    pline = msp.add_lwpolyline(pts, dxfattribs={"layer": "SITE_BOUNDARY"})
    pline.close(True)
    log.info(f"  DXF: SITE_BOUNDARY  {len(pts)} vertices")


def _write_view_points(
    msp,
    xs: list[float],
    ys: list[float],
    view_types: list[str],
    stride: int = 5,
) -> None:
    """
    Write VIEW_POINTS layer.
    stride: write one labelled point every N boundary points to keep
            DXF file size manageable (default: every 5m).
    """
    count = 0
    for i, (x, y, vt) in enumerate(zip(xs, ys, view_types)):
        if i % stride != 0:
            continue
        color = _VIEW_COLOR.get(vt, 3)
        msp.add_point(
            (x, y, 0),
            dxfattribs={"layer": "VIEW_POINTS", "color": color},
        )
        # Attach view label as TEXT entity
        msp.add_text(
            vt,
            dxfattribs={
                "layer":    "LABELS",
                "height":   0.5,
                "color":    color,
                "insert":   (x + 0.6, y + 0.6),
            },
        )
        count += 1

    log.info(f"  DXF: VIEW_POINTS  {count} points (stride={stride})")


def _write_noise_points(
    msp,
    xs: list[float],
    ys: list[float],
    noise_db: list[float],
    is_noisy: list[bool],
    db_threshold: float,
    stride: int = 5,
) -> None:
    """
    Write NOISE_POINTS layer.
    Noisy points (>= threshold) are written in red (color 1),
    quiet points in cyan (color 4).
    stride: write one labelled point every N boundary points.
    """
    count = 0
    for i, (x, y, db, noisy) in enumerate(zip(xs, ys, noise_db, is_noisy)):
        if i % stride != 0:
            continue
        color = 1 if noisy else 4
        msp.add_point(
            (x, y, 0),
            dxfattribs={"layer": "NOISE_POINTS", "color": color},
        )
        msp.add_text(
            f"{db:.1f}dB",
            dxfattribs={
                "layer":  "LABELS",
                "height": 0.5,
                "color":  color,
                "insert": (x + 0.6, y - 0.6),
            },
        )
        count += 1

    log.info(f"  DXF: NOISE_POINTS  {count} points (stride={stride})")


def _write_non_building_areas(
    msp,
    non_building_areas: dict,
) -> None:
    """
    Write NON_BUILDING layer.
    Each zone is written as a closed LWPOLYLINE with its description
    as an attached TEXT entity at the polygon centroid.
    """
    if not non_building_areas:
        return

    written = 0
    for colour_key, zone in non_building_areas.items():
        coords = zone.get("coordinates", {})
        zxs = coords.get("x", [])
        zys = coords.get("y", [])

        if len(zxs) < 3:
            log.warning(f"  DXF: NON_BUILDING zone {colour_key} has < 3 points — skipping")
            continue

        pts = list(zip(zxs, zys))
        pline = msp.add_lwpolyline(
            pts,
            dxfattribs={"layer": "NON_BUILDING", "color": 5},
        )
        pline.close(True)

        # Label at centroid
        cx = sum(zxs) / len(zxs)
        cy = sum(zys) / len(zys)
        label = zone.get("use", colour_key)[:30]   # truncate for DXF
        msp.add_text(
            label,
            dxfattribs={
                "layer":  "LABELS",
                "height": 1.0,
                "color":  5,
                "insert": (cx, cy),
            },
        )
        written += 1

    log.info(f"  DXF: NON_BUILDING  {written} zones")


def export_dxf(intelligence_data: dict) -> BytesIO:
    """
    Convert the site intelligence JSON dataset into a DXF file.

    Parameters
    ----------
    intelligence_data : dict returned by generate_site_intelligence()

    Returns
    -------
    BytesIO  — DXF file buffer (seeked to 0)
    """
    site_id = intelligence_data.get("site_id", "SITE")
    log.info(f"[dxf_export] START  site_id={site_id}")

    # ── Validate array lengths ────────────────────────────────
    boundary  = intelligence_data.get("boundary", {})
    xs        = boundary.get("x", [])
    ys        = boundary.get("y", [])
    view_types = intelligence_data.get("view_type", [])
    noise_db   = intelligence_data.get("noise_db", [])
    is_noisy   = intelligence_data.get("is_noisy", [])
    db_threshold = float(intelligence_data.get("db_threshold", 65.0))

    n = len(xs)
    if n == 0:
        raise ValueError("boundary.x is empty — cannot export DXF")

    # Pad arrays defensively if lengths mismatch (should not happen)
    view_types = (view_types + ["CITY"] * n)[:n]
    noise_db   = (noise_db   + [45.0]  * n)[:n]
    is_noisy   = (is_noisy   + [False] * n)[:n]

    # ── Create DXF document (R2010 = widely compatible) ───────
    doc = ezdxf.new(dxfversion="R2010")
    doc.header["$INSUNITS"] = 6      # metres
    doc.header["$LUNITS"]   = 2      # decimal
    doc.header["$LUPREC"]   = 4      # 4 decimal places

    _setup_layers(doc)
    msp = doc.modelspace()

    # ── Write title block comment ─────────────────────────────
    msp.add_text(
        f"ALKF MASTER LAND PLAN — {site_id}",
        dxfattribs={
            "layer":  "SITE_BOUNDARY",
            "height": 2.0,
            "color":  7,
            "insert": (min(xs) if xs else 0, min(ys) - 5 if ys else -5),
        },
    )
    msp.add_text(
        f"CRS: {intelligence_data.get('crs', 'EPSG:3857')}  "
        f"Sampling: {intelligence_data.get('sampling_interval_m', 1.0)}m  "
        f"Threshold: {db_threshold}dB  "
        f"Points: {n}",
        dxfattribs={
            "layer":  "SITE_BOUNDARY",
            "height": 1.0,
            "color":  8,
            "insert": (min(xs) if xs else 0, min(ys) - 8 if ys else -8),
        },
    )

    # ── Write geometry layers ─────────────────────────────────
    _write_site_boundary(msp, xs, ys)
    _write_view_points(msp, xs, ys, view_types, stride=5)
    _write_noise_points(msp, xs, ys, noise_db, is_noisy, db_threshold, stride=5)

    non_building = intelligence_data.get("non_building_areas", {})
    if non_building:
        _write_non_building_areas(msp, non_building)

    # ── Serialise to BytesIO ──────────────────────────────────
    buf = BytesIO()
    doc.write(buf)
    buf.seek(0)

    log.info(
        f"[dxf_export] DONE  "
        f"boundary={n}pts  "
        f"non_building={len(non_building)} zones  "
        f"size={buf.getbuffer().nbytes:,} bytes"
    )
    return buf
