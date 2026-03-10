# ALKF Master Land Plan API

**Boundary Intelligence Engine** — FastAPI microservice that walks a site boundary at 1-metre intervals and records view classification and noise level at every point, returning a structured JSON dataset or a DXF CAD file ready for import into AutoCAD, Rhino, or QGIS.

Part of the **ALKF+ Automated Spatial Intelligence Platform**.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Repository Structure](#repository-structure)
4. [Data Prerequisites](#data-prerequisites)
5. [Modules](#modules)
   - [spatial_intelligence.py](#spatial_intelligencepy)
   - [dxf_export.py](#dxf_exportpy)
   - [lease_plan_parser.py](#lease_plan_parserpy)
   - [resolver.py](#resolverpy-copied-from-alkf-site-analysis)
   - [view.py](#viewpy-copied-from-alkf-site-analysis)
   - [noise.py](#noisepy-copied-from-alkf-site-analysis)
6. [API Reference](#api-reference)
   - [GET /](#get-)
   - [POST /site-intelligence](#post-site-intelligence)
   - [POST /site-intelligence-dxf](#post-site-intelligence-dxf)
7. [Request Model](#request-model)
8. [Response Schema](#response-schema)
   - [Basic Response](#basic-response)
   - [Extended Response (with Lease Plan)](#extended-response-with-lease-plan)
9. [DXF Output Specification](#dxf-output-specification)
10. [Algorithms](#algorithms)
    - [Boundary Densification](#boundary-densification)
    - [View Classification](#view-classification)
    - [Noise Sampling](#noise-sampling)
    - [Lease Plan Colour Segmentation](#lease-plan-colour-segmentation)
11. [Caching](#caching)
12. [Deployment](#deployment)
    - [Local Development](#local-development)
    - [Render Cloud](#render-cloud)
13. [Dependencies](#dependencies)
14. [Environment Notes](#environment-notes)
15. [Testing](#testing)
16. [Known Limitations](#known-limitations)
17. [Relationship to alkf-site-analysis](#relationship-to-alkf-site-analysis)

---

## Overview

Given any supported site identifier (lot number, address with coordinates, or CSUID), the Boundary Intelligence Engine:

1. Resolves the identifier to WGS84 coordinates via the HK GeoData API
2. Retrieves the official lot boundary polygon from the LandsD iC1000 API (GML → EPSG:3857)
3. Densifies the boundary exterior at **1-metre intervals** using Shapely interpolation
4. Classifies the dominant **view type** at each boundary point (`SEA`, `HARBOR`, `RESERVOIR`, `GREEN`, `PARK`, `CITY`) using the view sector model from `view.py`
5. Samples the **road traffic noise level** (dBA) at each boundary point using the full noise propagation pipeline from `noise.py`, with a vectorised fallback model if the WFS pipeline fails
6. Evaluates each point against a configurable **noise threshold** (default 65 dBA per HK EPD)
7. Optionally extracts **non-building zone polygons** from a lease plan image or PDF using OpenCV colour segmentation, and maps pixel coordinates to EPSG:3857
8. Returns either a structured **JSON dataset** or a **DXF CAD file** containing all of the above as named layers

---

## Architecture

```
Client Request
      │
      ▼
┌─────────────────────────────────────────────────────────────┐
│  FastAPI  (app.py)                                          │
│                                                             │
│  POST /site-intelligence        → JSONResponse              │
│  POST /site-intelligence-dxf   → StreamingResponse (.dxf)   │
│  GET  /                         → health check              │
│                                                             │
│  In-memory cache (CACHE_STORE)                              │
│    key: MD5(data_type + value + db_threshold)               │
│    lease_plan requests: never cached                        │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  generate_site_intelligence()  (spatial_intelligence.py)    │
│                                                             │
│  Step 1  resolve_location()     → (lon, lat) WGS84          │
│          get_lot_boundary()     → Polygon EPSG:3857         │
│          fallback 1: OSM building polygon nearest site      │
│          fallback 2: 40m circular buffer                    │
│                                                             │
│  Step 2  _densify_boundary()                                │
│          Shapely exterior.interpolate(d) every 1m           │
│          → (xs, ys) parallel lists, EPSG:3857               │
│                                                             │
│  Step 3  _fetch_view_features()                             │
│          OSMnx features_from_point(radius=500m)             │
│          buildings / parks / water                          │
│                                                             │
│  Step 4  _batch_classify_views()                            │
│          ≤500 pts → direct per-point classification         │
│          >500 pts → 10m grid sample + NN assignment         │
│          calls view.py: _classify_sectors() _get_site_height│
│                                                             │
│  Step 5  _build_noise_grid()                                │
│          Full noise.py pipeline:                            │
│          ATCWFSLoader → TrafficAssigner → LNRSAssigner      │
│          → CanyonAssigner → EmissionEngine                  │
│          → PropagationEngine.run() → (X, Y, noise[i,j])     │
│          _sample_noise_at_points() — NN grid lookup         │
│          fallback: _fallback_noise_from_roads() vectorised  │
│                                                             │
│  Step 6  is_noisy = [v >= db_threshold for v in noise_db]   │
│                                                             │
│  Step 7  (Optional) lease_plan_parser                       │
│          .extract_non_building_areas()                      │
│          → HSV segmentation → contours → EPSG:3857 coords   │
│                                                             │
│  → return dict (JSON-serialisable)                          │
└───────────────────────┬─────────────────────────────────────┘
                        │
              ┌─────────┴──────────┐
              ▼                    ▼
        JSONResponse          export_dxf()
                              ezdxf R2010
                              → BytesIO → StreamingResponse
```

### Static Data (Startup Preload)

At startup, `app.py` loads one dataset into memory:

| Dataset | File | Rows | Filtered to | Used by |
|---|---|---|---|---|
| Building heights | `data/BUILDINGS_FINAL.gpkg` | 42,073 | `HEIGHT_M > 5m` | `spatial_intelligence._batch_classify_views()` |

`ZONE_REDUCED.gpkg` is **not loaded** in this repository — context/zoning analysis is not part of the MLP output.

---

## Repository Structure

```
alkf-master-land-plan/
│
├── app.py                        # FastAPI application — endpoints, cache, startup
├── render.yaml                   # Render cloud deployment configuration
├── requirements.txt              # Python dependencies (unpinned for Python 3.11)
├── runtime.txt                   # python-3.11.4
│
├── data/
│   └── BUILDINGS_FINAL.gpkg      # ← MUST BE COPIED from alkf-site-analysis/data/
│
└── modules/
    ├── __init__.py
    │
    │   ── NEW modules (this repository) ──────────────────────
    ├── spatial_intelligence.py   # Core pipeline orchestrator
    ├── dxf_export.py             # DXF CAD writer (ezdxf R2010)
    ├── lease_plan_parser.py      # OpenCV colour segmentation engine
    │
    │   ── COPIED from alkf-site-analysis/modules/ ────────────
    ├── resolver.py               # Multi-type location resolver + iC1000 boundary API
    ├── view.py                   # 360° view sector classification engine
    └── noise.py                  # Road traffic noise propagation model
```

> **Three files must be copied manually** before first run. See [Data Prerequisites](#data-prerequisites).

---

## Data Prerequisites

This repository depends on files from the `alkf-site-analysis` repository. They are not committed here to avoid duplication.

### Step 1 — Copy modules

```bash
cp ../alkf-site-analysis/modules/resolver.py  ./modules/resolver.py
cp ../alkf-site-analysis/modules/view.py       ./modules/view.py
cp ../alkf-site-analysis/modules/noise.py      ./modules/noise.py
```

### Step 2 — Copy data

```bash
cp ../alkf-site-analysis/data/BUILDINGS_FINAL.gpkg  ./data/BUILDINGS_FINAL.gpkg
```

`BUILDINGS_FINAL.gpkg` contains 42,073 building footprint polygons with a `HEIGHT_M` column in EPSG:3857, pre-filtered from the full LandsD dataset (~342,000 rows). The `view.py` module requires it at startup.

### Step 3 — (Optional) Poppler for PDF lease plans

If lease plan inputs will be PDF files, install Poppler on the deployment host:

```bash
# Ubuntu / Render
apt-get install -y poppler-utils

# macOS
brew install poppler
```

PNG and JPEG lease plans work without Poppler.

---

## Modules

### `spatial_intelligence.py`

The core pipeline orchestrator. Contains all internal functions and the single public entry point `generate_site_intelligence()`.

#### Public interface

```python
def generate_site_intelligence(
    data_type:          str,
    value:              str,
    building_data:      gpd.GeoDataFrame,
    lon:                Optional[float] = None,
    lat:                Optional[float] = None,
    lot_ids:            Optional[list]  = None,
    extents:            Optional[list]  = None,
    db_threshold:       float           = 65.0,
    non_building_json:  Optional[dict]  = None,
    lease_plan_b64:     Optional[str]   = None,
) -> dict
```

Returns a JSON-serialisable dict. See [Response Schema](#response-schema).

#### Internal functions

| Function | Description |
|---|---|
| `_densify_boundary(polygon, interval_m)` | Interpolates points along polygon exterior every `interval_m` metres. Returns `(xs, ys)` lists in EPSG:3857. |
| `_fetch_view_features(lon, lat, radius_m)` | Fetches OSM buildings, parks, and water within radius. Returns dict of GeoDataFrames. |
| `_classify_view_at_point(x, y, features, building_data, radius_m)` | Classifies dominant view type at a single point. Calls `view.py` internals via lazy import. |
| `_batch_classify_views(xs, ys, features, building_data, radius_m)` | Classifies view at all boundary points. Uses direct mode (≤500 pts) or grid + nearest-neighbour (>500 pts). |
| `_build_noise_grid(lon, lat, site_polygon, cfg)` | Runs full `noise.py` pipeline. Returns `(X, Y, noise)` arrays or `(None, None, None)` on failure. |
| `_sample_noise_at_points(xs, ys, X, Y, noise)` | Samples the noise grid at each boundary point via nearest-neighbour index lookup. |
| `_fallback_noise_from_roads(xs, ys, lon, lat)` | Lightweight fallback: fetches OSM roads within 300m and applies point-source attenuation `L = L₀ − 20·log₁₀(d+1)` using vectorised NumPy operations. |

#### Import strategy

`view.py` and `noise.py` import `matplotlib`, `contextily`, and `scikit-learn` at module level. To avoid triggering those imports at startup, all calls into those modules use **lazy imports inside the consuming function**:

```python
def _classify_view_at_point(...):
    from modules.view import _classify_sectors, _get_site_height  # loaded on first call only
    ...

def _build_noise_grid(...):
    from modules.noise import ATCWFSLoader, PropagationEngine, ...  # loaded on first call only
    ...
```

---

### `dxf_export.py`

Converts the site intelligence JSON dict into a DXF file using `ezdxf`.

#### Public interface

```python
def export_dxf(site_intelligence: dict) -> BytesIO
```

Returns a `BytesIO` buffer containing a valid DXF R2010 file.

#### DXF document settings

| Setting | Value | Purpose |
|---|---|---|
| DXF version | `R2010` (AC1024) | Broadest compatibility (AutoCAD 2010+, Rhino, QGIS) |
| `$INSUNITS` | `6` | Metres |
| `$LUNITS` | `2` | Decimal |

#### Layer definitions

| Layer | ACI Colour | Linetype | Content |
|---|---|---|---|
| `SITE_BOUNDARY` | 7 (white/black) | CONTINUOUS | Closed LWPOLYLINE of all boundary points |
| `VIEW_POINTS` | 3 (green) | CONTINUOUS | POINT entities at every 5th boundary point |
| `NOISE_POINTS` | 1 (red) | CONTINUOUS | POINT entities at every 5th boundary point |
| `NON_BUILDING` | 5 (blue) | DASHED | Closed LWPOLYLINE per extracted zone |
| `LABELS` | 2 (yellow) | CONTINUOUS | TEXT entities — zone names at polygon centroids |

#### Per-point colour mapping

VIEW_POINTS entities are coloured by view type via ACI index override:

| View Type | ACI Colour |
|---|---|
| `SEA` / `HARBOR` / `RESERVOIR` | 4 (cyan) |
| `GREEN` / `PARK` | 3 (green) |
| `CITY` | 1 (red) |
| `OPEN` | 2 (yellow) |
| `MOUNTAIN` | 8 (grey) |

NOISE_POINTS entities:
- `is_noisy = True` → ACI 1 (red)
- `is_noisy = False` → ACI 4 (cyan)

#### Title block

A minimal text title block is written at `(min(xs), min(ys) − 5)` on the `LABELS` layer containing: site ID, CRS, point count, noise threshold, and generation timestamp.

---

### `lease_plan_parser.py`

Extracts non-building zone polygons from a lease plan image or PDF using OpenCV colour segmentation in HSV colour space.

#### Public interface

```python
def extract_non_building_areas(
    image_bytes:        bytes,
    non_building_json:  dict,
    site_polygon:       Polygon,
    crs:                str = "EPSG:3857",
) -> dict
```

Returns a dict keyed by normalised colour label (e.g. `"pink_cross_hatched_black"`), each containing zone metadata and EPSG:3857 coordinates.

#### Pipeline

```
image_bytes (PDF or PNG/JPEG)
      │
      ├── PDF → pdf2image → rasterised PIL Image → NumPy BGR array
      └── PNG/JPEG → cv2.imdecode → NumPy BGR array
                        │
                        ▼
              Convert BGR → HSV
                        │
                        ▼
              For each colour label in non_building_json:
              ┌────────────────────────────────────────┐
              │ _extract_base_colour()                 │
              │   "pink_cross_hatched_black" → "pink"  │
              │                                        │
              │ cv2.inRange(hsv, lower, upper) → mask  │
              │ (red uses dual-range due to H wrap)    │
              │                                        │
              │ Morphological cleanup:                 │
              │   MORPH_CLOSE (5×5 ellipse) → fill gaps│
              │   MORPH_OPEN  (5×5 ellipse) → denoise  │
              │                                        │
              │ cv2.findContours(RETR_EXTERNAL)        │
              │ Filter: area >= 200 px                 │
              │ approxPolyDP(ε = 0.002 × arc_length)   │
              │                                        │
              │ _pixel_to_geo():                       │
              │   pixel (0,0)   = (site_bbox.minx,     │
              │                    site_bbox.maxy)     │
              │   pixel (w,h)   = (site_bbox.maxx,     │
              │                    site_bbox.miny)     │
              │   north-up linear interpolation        │
              └────────────────────────────────────────┘
                        │
                        ▼
              Return structured dict
```

#### Supported colour labels

| Base colour | HSV range (H·S·V) | Notes |
|---|---|---|
| `pink` | H[140–175], S[30–160], V[150–255] | |
| `green` | H[35–90], S[40–255], V[40–255] | |
| `blue` | H[90–130], S[50–255], V[50–255] | |
| `yellow` | H[20–35], S[80–255], V[80–255] | |
| `red` | H[0–10] ∪ H[165–179], S[50–255], V[50–255] | Dual-range, H wraps at 180 |
| `orange` | H[10–20], S[80–255], V[80–255] | |
| `purple` | H[125–145], S[30–255], V[50–255] | |
| `grey` | H[0–179], S[0–40], V[80–200] | |
| `white` | H[0–179], S[0–30], V[200–255] | |
| `black` | H[0–179], S[0–255], V[0–50] | |

Composite keys such as `"pink cross-hatched black"` are accepted — the base colour (`"pink"`) is extracted by matching the first token against the colour table.

---

### `resolver.py` *(copied from alkf-site-analysis)*

Multi-type location resolver. Translates a `data_type` + `value` pair into WGS84 coordinates and retrieves the official lot boundary polygon from the LandsD iC1000 API.

**Coordinate transform chain:** EPSG:2326 (HK1980 Grid) → EPSG:4326 (WGS84) → EPSG:3857 (Web Mercator) via PyProj.

**Supported `data_type` values:**

| `data_type` | Description | Boundary source |
|---|---|---|
| `LOT` | Inland lot (IL, NKIL, KIL, etc.) | LandsD iC1000 `lot` endpoint |
| `STT` | Short-term tenancy lot | iC1000 `stt` endpoint |
| `GLA` | Government land allocation | iC1000 `gla` endpoint |
| `LPP` | Licence / permit parcel | iC1000 `lpp` endpoint |
| `UN` | Utility notation | iC1000 `un` endpoint |
| `BUILDINGCSUID` | Building CSUID | iC1000 |
| `LOTCSUID` | Lot CSUID | iC1000 |
| `PRN` | Property reference number | iC1000 |
| `ADDRESS` | Pre-resolved | Pass `lon`/`lat` directly; boundary always `None` |

---

### `view.py` *(copied from alkf-site-analysis)*

360° view sector classification engine. Divides the 360° horizon into `SECTOR_SIZE`-degree wedges and scores each as `GREEN`, `WATER`, `CITY`, or `OPEN` based on green-space ratio, water-body ratio, and building height/density relative to site reference height.

Used in this repository via its internal functions `_classify_sectors()` and `_get_site_height()` (lazy imported).

**Sector scoring model:**

```
Green Score  = green_ratio                        → label: GREEN
Water Score  = water_ratio                        → label: WATER (remapped → SEA)
City Score   = height_norm × density_norm         → label: CITY
Open Score   = (1 − density_norm) × (1 − height_norm) → label: OPEN
Priority: CITY > WATER > GREEN > OPEN
```

**View label remapping** (applied in `spatial_intelligence.py`):

| `view.py` output | MLP label | Refinement condition |
|---|---|---|
| `GREEN` | `GREEN` | — |
| `WATER` | `SEA` | Default |
| `WATER` | `RESERVOIR` | OSM tag `landuse=reservoir` detected |
| `WATER` | `HARBOR` | OSM tag `natural=harbour` detected |
| `OPEN` | `GREEN` | Remapped to GREEN |
| `CITY` | `CITY` | — |

---

### `noise.py` *(copied from alkf-site-analysis)*

Road traffic noise propagation model. Full pipeline used in this repository:

```
ATCWFSLoader     — fetches ATC traffic census data from CSDI WFS
LNRSWFSLoader    — fetches LNRS (Low Noise Road Surface) data from CSDI WFS
TrafficAssigner  — snaps ATC stations to road segments (threshold: 500m)
LNRSAssigner     — applies −3 dB correction to LNRS-designated segments
CanyonAssigner   — adds canyon reflection bonus (up to +8 dB) for enclosed roads
EmissionEngine   — computes L_link (dBA) per road segment using EPD empirical formula
PropagationEngine.run() — accumulates noise on a 5m resolution grid
                          applies Gaussian smoothing (σ=1.5)
                          returns (X[grid], Y[grid], noise[i,j])
```

**Base emission model (EPD Hong Kong):**

```
L₀ = A + B·log₁₀(Q) + correction_terms
```

Where `Q` = flow (vehicles/hour), and correction terms include heavy vehicle fraction, speed adjustment, ground absorption, and canyon reflection.

**Fallback model** (when WFS pipeline fails — network unavailable or no roads in radius):

```
L(r) = L_road_class − 20·log₁₀(r + 1)
```

Vectorised across all road segments using NumPy broadcasting. Road-class base levels: motorway 82 dB, primary 74 dB, secondary 70 dB, residential 60 dB.

---

## API Reference

### Base URL

```
https://alkf-master-land-plan.onrender.com
```

### `GET /`

Health check.

**Response:**

```json
{
  "service": "ALKF Master Land Plan API",
  "version": "1.0",
  "status":  "operational"
}
```

---

### `POST /site-intelligence`

Runs the full boundary intelligence pipeline and returns a structured JSON dataset.

**Content-Type:** `application/json`  
**Response:** `200 OK` — `application/json`

**Errors:**

| Code | Reason |
|---|---|
| `422` | `ADDRESS` type supplied without `lon`/`lat`; invalid request body |
| `500` | Resolver failure; OSM fetch failure; geometry processing error |

---

### `POST /site-intelligence-dxf`

Identical computation to `/site-intelligence`. Returns a DXF R2010 file as a binary download.

**Content-Type:** `application/json`  
**Response:** `200 OK` — `application/dxf`  
**Content-Disposition:** `attachment; filename="{site_id}_boundary_intelligence.dxf"`

**Errors:** same as `/site-intelligence`, plus `500` if the DXF serialisation stage fails.

---

## Request Model

```json
{
  "data_type":          "LOT",
  "value":              "IL 1657",
  "lon":                null,
  "lat":                null,
  "lot_ids":            null,
  "extents":            null,
  "db_threshold":       65.0,
  "non_building_json":  null,
  "lease_plan_b64":     null
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `data_type` | string | ✅ | `LOT`, `STT`, `GLA`, `LPP`, `ADDRESS`, etc. Case-insensitive. |
| `value` | string | ✅ | Identifier value, e.g. `"IL 1657"` |
| `lon` | float | ✅ for ADDRESS | Pre-resolved WGS84 longitude (from `/search` on alkf-site-analysis) |
| `lat` | float | ✅ for ADDRESS | Pre-resolved WGS84 latitude |
| `lot_ids` | string[] | ○ | Multi-lot identifiers (reserved for future use) |
| `extents` | object[] | ○ | Multi-lot EPSG:2326 bounding boxes `{xmin, ymin, xmax, ymax}` |
| `db_threshold` | float | ○ | Noise threshold in dBA. Default: `65.0` (HK EPD day limit) |
| `non_building_json` | object | ○ | Colour label definitions. Required alongside `lease_plan_b64` to activate zone extraction. |
| `lease_plan_b64` | string | ○ | Base64-encoded lease plan (PDF, PNG, or JPEG). Activates `non_building_areas` in response. |

### `non_building_json` schema

```json
{
  "color_labels": {
    "pink": {
      "description": "Site area",
      "reference_clause": "PARTICULARS OF THE LOT"
    },
    "pink cross-hatched black": {
      "height": "5.1 metres",
      "description": "Drainage Reserve Area",
      "reference_clause": "Drainage Reserve Area"
    },
    "green": {
      "description": "Future public roads (the Green Area)",
      "reference_clause": "Formation of the Green Area"
    }
  },
  "non_building_areas": [
    {
      "description": "Drainage Reserve Area",
      "location_ref": "shown coloured pink cross-hatched black on the plan annexed hereto",
      "reference_clause": "Drainage Reserve Area"
    },
    {
      "description": "Future public roads",
      "location_ref": "shown coloured green on the plan annexed hereto",
      "reference_clause": "Formation of the Green Area"
    }
  ]
}
```

Only entries listed in `non_building_areas` are extracted from the lease plan. `color_labels` provides the metadata attached to each extracted zone.

---

## Response Schema

### Basic Response

Returned when `lease_plan_b64` is absent or when no `non_building_json` is provided.

```json
{
  "site_id":             "IL_1657",
  "crs":                 "EPSG:3857",
  "sampling_interval_m": 1.0,
  "boundary": {
    "x": [12700123.4, 12700124.3, "..."],
    "y": [2560234.1,  2560235.0,  "..."]
  },
  "view_type":    ["SEA", "CITY", "GREEN", "..."],
  "noise_db":     [62.3,  71.8,   58.4,    "..."],
  "db_threshold": 65.0,
  "is_noisy":     [false, true,   false,   "..."]
}
```

| Field | Type | Description |
|---|---|---|
| `site_id` | string | Normalised identifier: `value.upper().replace(" ", "_")` |
| `crs` | string | Always `"EPSG:3857"` (Web Mercator, metres) |
| `sampling_interval_m` | float | Always `1.0` |
| `boundary.x` | float[] | Easting coordinates of boundary points (metres) |
| `boundary.y` | float[] | Northing coordinates of boundary points (metres) |
| `view_type` | string[] | Per-point view classification. Values: `SEA`, `HARBOR`, `RESERVOIR`, `GREEN`, `PARK`, `CITY`, `MOUNTAIN` |
| `noise_db` | float[] | Per-point noise level in dBA, rounded to 1 decimal place |
| `db_threshold` | float | The threshold used to compute `is_noisy` |
| `is_noisy` | bool[] | `true` where `noise_db[i] >= db_threshold` |

All arrays (`boundary.x`, `boundary.y`, `view_type`, `noise_db`, `is_noisy`) are guaranteed to have identical length.

---

### Extended Response (with Lease Plan)

When both `non_building_json` and `lease_plan_b64` are provided, the response includes an additional `non_building_areas` key:

```json
{
  "site_id":             "IL_1657",
  "crs":                 "EPSG:3857",
  "sampling_interval_m": 1.0,
  "boundary":     { "x": ["..."], "y": ["..."] },
  "view_type":    ["..."],
  "noise_db":     ["..."],
  "db_threshold": 65.0,
  "is_noisy":     ["..."],

  "non_building_areas": {
    "pink_cross_hatched_black": {
      "use":               "Drainage Reserve Area",
      "reference_clause":  "Drainage Reserve Area",
      "location_ref":      "shown coloured pink cross-hatched black on the plan",
      "height":            "5.1 metres",
      "coordinates": {
        "x": [12700200.1, 12700210.4, 12700215.2, "..."],
        "y": [2560300.0,  2560298.5,  2560295.1,  "..."]
      }
    },
    "green": {
      "use":               "Future public roads (the Green Area)",
      "reference_clause":  "Formation of the Green Area",
      "location_ref":      "shown coloured green on the plan",
      "height":            null,
      "coordinates": {
        "x": ["..."],
        "y": ["..."]
      }
    }
  }
}
```

`non_building_areas` is a dict keyed by the normalised colour label (spaces and hyphens replaced with underscores, lowercased). Each zone has at least 3 coordinate points forming a closed polygon.

---

## DXF Output Specification

The DXF file produced by `/site-intelligence-dxf` conforms to the AutoCAD R2010 (AC1024) format.

### Document units

```
$INSUNITS = 6    (metres)
$LUNITS   = 2    (decimal)
```

All coordinates are in EPSG:3857 (metric Web Mercator). To overlay with other GIS data in AutoCAD or QGIS, set the drawing CRS to EPSG:3857.

### Layer summary

| Layer | Entities | Stride | Note |
|---|---|---|---|
| `SITE_BOUNDARY` | 1 closed LWPOLYLINE | — | All boundary points |
| `VIEW_POINTS` | N/5 POINT entities | Every 5th point | ACI colour per view type |
| `NOISE_POINTS` | N/5 POINT entities | Every 5th point | Red = noisy, cyan = quiet |
| `NON_BUILDING` | 1 LWPOLYLINE per zone | — | Only if lease plan provided |
| `LABELS` | TEXT entities | — | Zone names + title block |

### Importing into CAD software

**AutoCAD / BricsCAD:**
```
File → Open → select .dxf → units: metres
```

**Rhino:**
```
File → Import → DXF/DWG → import units: metres
```

**QGIS:**
```
Layer → Add Layer → Add Vector Layer → select .dxf
Set CRS to EPSG:3857 on import
```

---

## Algorithms

### Boundary Densification

Given a lot polygon in EPSG:3857, the boundary is densified by walking the exterior at constant 1-metre intervals:

```python
exterior  = polygon.exterior          # Shapely LinearRing
length    = exterior.length           # perimeter in metres
n         = int(floor(length / 1.0))  # number of sample points
distances = linspace(0.0, length - 1.0, n)

for d in distances:
    pt = exterior.interpolate(d)      # Shapely returns point at arc distance d
    xs.append(pt.x)
    ys.append(pt.y)
```

The last point is offset by 1m from the closure to avoid exact duplication of the first point. For MultiPolygon inputs, the largest sub-polygon is used.

### View Classification

Per-point view classification is a two-stage operation:

**Stage 1 — Feature fetch** (once per request, not per point):
OSMnx fetches buildings, parks, and water features within 500m of the site centroid. All geometries are projected to EPSG:3857.

**Stage 2 — Per-point classification:**

For each boundary point, a 200m radius analysis circle is constructed. The view sector model from `view.py` divides this into 20° wedges and scores each:

```
For each 20° wedge:
  green_share = parks.intersection(wedge).area / wedge.area
  water_share = water.intersection(wedge).area / wedge.area
  is_city     = any building in wedge with HEIGHT_M > h_ref

  Priority: CITY > WATER (if > 2%) > GREEN (if > 2%) > OPEN
```

**Performance scaling:**
- Boundaries with ≤500 points: direct per-point classification
- Boundaries with >500 points: view computed on a 10m grid, then assigned to each boundary point via nearest-neighbour lookup (eliminates redundant computation on large, low-curvature boundaries)

### Noise Sampling

The full `noise.py` pipeline produces a 5m-resolution noise grid over the study area (150m radius). Each boundary point is sampled from this grid via nearest-neighbour index lookup:

```python
xi = argmin(abs(grid_x_values - point_x))
yi = argmin(abs(grid_y_values - point_y))
db = noise_grid[yi, xi]
```

If the WFS pipeline fails (CSDI API unavailable, no roads in radius, or propagation error), a fallback model is applied:

```
For each road segment within 300m:
  L_seg(r) = L_road_class - 20·log₁₀(r + 1)

  Total at boundary point:
  L_total = 10·log₁₀(Σ 10^(L_seg/10))
```

Computed vectorised across all boundary points and road segments simultaneously using NumPy broadcasting with a chunk size of 200 boundary points.

### Lease Plan Colour Segmentation

Pixel-to-geographic coordinate mapping assumes a **north-up** image orientation with linear scale:

```
geo_x = site_bbox.minx + (pixel_col / image_width)  × (site_bbox.maxx - site_bbox.minx)
geo_y = site_bbox.maxy - (pixel_row / image_height) × (site_bbox.maxy - site_bbox.miny)
```

Contours are simplified using the Douglas-Peucker algorithm with ε = 0.002 × contour arc length. Contours with area < 200px are discarded as noise.

---

## Caching

Responses are cached in-process in a Python dict (`CACHE_STORE`):

```
Cache key = MD5( data_type + "_" + value + "_" + db_threshold )
```

Cache behaviour:

| Condition | Cached |
|---|---|
| Standard request (no lease plan) | ✅ Cached after first computation |
| Request with `lease_plan_b64` | ❌ Never cached — file content may differ |
| Different `db_threshold` for same site | ❌ Cache miss — different key |

The cache is in-memory only and is cleared on server restart. On Render free tier, the server sleeps after inactivity and cache is lost on wake.

---

## Deployment

### Local Development

```bash
# 1. Clone and set up environment
git clone https://github.com/your-org/alkf-master-land-plan.git
cd alkf-master-land-plan
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# 3. Copy required files from alkf-site-analysis
cp ../alkf-site-analysis/modules/resolver.py  ./modules/
cp ../alkf-site-analysis/modules/view.py       ./modules/
cp ../alkf-site-analysis/modules/noise.py      ./modules/
cp ../alkf-site-analysis/data/BUILDINGS_FINAL.gpkg  ./data/

# 4. Start the server
uvicorn app:app --host 0.0.0.0 --port 10000 --reload
```

API will be available at `http://localhost:10000`.  
Interactive docs (Swagger UI): `http://localhost:10000/docs`

### Render Cloud

`render.yaml` is pre-configured for Render deployment:

```yaml
services:
  - type: web
    name: alkf-master-land-plan
    env: python
    region: singapore
    buildCommand: pip install --upgrade pip setuptools wheel && pip install -r requirements.txt
    startCommand: uvicorn app:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.4
    autoDeploy: true
```

**Deploy steps:**
1. Push repository to GitHub (include `data/BUILDINGS_FINAL.gpkg` and all three copied module files)
2. Connect repository to Render dashboard
3. Render auto-detects `render.yaml` and deploys

> **Note:** `BUILDINGS_FINAL.gpkg` is 64MB. If it exceeds GitHub's file size limit, use Git LFS or upload via Render's persistent disk.

**Cold start time (free tier):** 30–90 seconds (server sleeps after 15 min inactivity).  
**First request after wake:** includes OSMnx fetch + noise pipeline + view classification — expect 15–45 seconds depending on site complexity.  
**Cached request:** < 2 seconds.

---

## Dependencies

```
# Build system
setuptools

# API framework
fastapi
uvicorn[standard]

# Geospatial core
geopandas
shapely
pyproj
fiona
osmnx

# Numerical
numpy
scipy
pandas
networkx

# Visualisation (required by view.py and noise.py internals)
matplotlib
contextily
Pillow
scikit-learn

# HTTP
requests

# DXF export
ezdxf

# Lease plan parsing
opencv-python-headless
pdf2image
```

`contextily`, `matplotlib`, `Pillow`, and `scikit-learn` are required by `view.py` and `noise.py` even though this repository does not produce any map images. They are loaded lazily and do not affect startup time.

---

## Environment Notes

### Python version

Python 3.11 is required. The geospatial stack (`shapely`, `fiona`, `geopandas`, `pyproj`) relies on prebuilt C-extension wheels. These wheels exist on PyPI for Python 3.8–3.12. Python 3.13 and 3.14 do not yet have complete wheel coverage for this stack. `runtime.txt` pins `python-3.11.4`.

### pkg_resources on Python 3.12+

`pkg_resources` was removed from the Python standard library in Python 3.12. It is provided by `setuptools`. `setuptools` is listed as an explicit dependency to ensure it is always present.

### Poppler (PDF lease plans only)

`pdf2image` requires `poppler-utils` to rasterise PDF files. Install on the deployment host if PDF lease plans will be used. PNG and JPEG inputs do not require Poppler.

---

## Testing

A complete Google Colab test notebook is provided: `ALKF_MLP_API_Test.ipynb`.

It covers:

| Test | What it validates |
|---|---|
| Health check | Service is alive and returns expected keys |
| LOT → JSON | Full basic output including all array fields |
| LOT → DXF | File downloads, valid DXF header detected |
| ADDRESS type | Pre-resolved coordinate path works |
| Custom threshold | `db_threshold=55.0` applied and reflected in `is_noisy` |
| Array length consistency | `boundary.x`, `view_type`, `noise_db`, `is_noisy` all identical length |
| Coordinate sanity | All points within HK EPSG:3857 bounding box |
| View type labels | Only valid labels returned |
| Noise value validation | All values in [20, 120] dB; `is_noisy` consistent with threshold |
| Cache verification | Second call faster than first |
| Error — ADDRESS without lon/lat | Returns `422` |
| Error — invalid LOT | Returns `422` or `500` |
| Non-building JSON without lease plan | `non_building_areas` absent from response |
| Full lease plan extraction | `non_building_areas` present, each zone has ≥3 coordinate points |

Set `BASE_URL` in cell 2 before running:

```python
BASE_URL = "https://alkf-master-land-plan.onrender.com"
```

---

## Known Limitations

**Noise model accuracy**  
The noise propagation model is a near-field screening model based on EPD empirical formulae. It is not ISO 9613-2 compliant, has not been calibrated against field measurements, and should not be used for formal Environmental Impact Assessment submissions.

**View classification radius**  
Each boundary point uses a fixed 200m analysis radius. For very large sites (perimeter > 2000m), the view at inner boundary segments may be influenced by features that are functionally further away in the urban fabric.

**Lease plan coordinate alignment**  
The `_pixel_to_geo()` mapping assumes that the lease plan image is a north-up, scale-correct orthographic projection aligned exactly with the site bounding box. Scanned lease plans that are rotated, distorted, or at an oblique angle will produce incorrect zone coordinates.

**In-memory cache**  
The cache is not shared across worker processes. On Render free tier (single process), this is not a concern. On multi-worker deployments, use Redis or a persistent key-value store.

**CSDI WFS availability**  
Both the ATC traffic census (`atc_wfs_url`) and LNRS road surface data (`lnrs_wfs_url`) are fetched from CSDI HK government WFS endpoints. These services have intermittent downtime. When unavailable, the noise module falls back to the road-class attenuation model automatically.

---

## Relationship to alkf-site-analysis

| | `alkf-site-analysis` | `alkf-master-land-plan` |
|---|---|---|
| **Purpose** | Site feasibility maps + PDF report | Boundary-sampled JSON / DXF dataset |
| **Output** | PNG maps, PDF report | JSON array data, DXF CAD file |
| **Spatial unit** | Whole-site analysis, single output per module | Per-point data at 1m intervals along boundary |
| **View module** | Renders polar diagram image | Uses internal functions only — no image |
| **Noise module** | Renders heatmap image | Samples grid values per boundary point |
| **Zoning data** | `ZONE_REDUCED.gpkg` loaded and used | Not loaded or used |
| **Visualisation libs** | matplotlib, contextily used for output | Required as deps only (via copied modules) |
| **New modules** | — | `spatial_intelligence.py`, `dxf_export.py`, `lease_plan_parser.py` |
| **Shared modules** | `resolver.py`, `view.py`, `noise.py` | Same files (copied, not modified) |

The two APIs are designed to be used together: `alkf-site-analysis` provides the analyst with visual feasibility maps; `alkf-master-land-plan` provides the architect or planner with structured data and CAD-ready geometry for the next stage of design.

---

## Changelog

### v1.1
- Replaced top-level imports from `view.py` and `noise.py` with lazy imports inside consuming functions — eliminates `ModuleNotFoundError: No module named 'contextily'` on startup
- Added `matplotlib`, `contextily`, `Pillow`, `scikit-learn` to `requirements.txt`
- Updated `render.yaml` build command to upgrade `pip`, `setuptools`, and `wheel` before install
- Pinned to Python 3.11.4 via `runtime.txt` and `PYTHON_VERSION` env var for wheel compatibility

### v1.0
- Initial release — boundary densification, view classification, noise sampling, DXF export, lease plan colour segmentation
