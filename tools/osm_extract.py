#!/usr/bin/env python3
"""Offline OpenStreetMap speed-limit extract (Overpass API → compact JSON).

    tools/osm_extract.py [--bbox S,W,N,E | --center LAT,LON --size-km K] [-o OUT.json.gz]

Downloads, in **one** Overpass query, every drivable ``highway=*`` way in the
box with its geometry and the tags the speed-limit matcher needs
(``maxspeed``, ``maxspeed:forward/backward``, ``maxspeed:type``,
``source:maxspeed``, ``zone:traffic``, ``zone:maxspeed``, ``highway``,
``name``, ``ref``, ``oneway``, ``junction``, ``lanes``, ``motorroad``,
``maxspeed:conditional``) plus the ``place=*`` nodes (city … hamlet) used as
a built-up-area hint, and writes a compact (optionally gzipped) JSON extract::

    {"format": "osm-speed-extract", "version": 1, "scale": 100000,
     "bbox": [S, W, N, E], "created": "...", "source": "overpass ...",
     "ways":   [{"id": 123, "c": [lat0, lon0, lat1, lon1, ...], "t": {tag: value}}],
     "places": [{"id": 9, "c": [lat, lon], "place": "city", "name": "Gdańsk"}]}

Coordinates are integers in 1e-5 degrees (≈1.1 m). Ways that share a vertex
share the exact integer pair, which is how the loader finds junctions.

Default box: ~15×15 km around Gdańsk (54.35 N, 18.65 E). Be polite: one
request, a descriptive User-Agent, and on HTTP 429/5xx or a timeout a retry
with exponential backoff (30, 60, 120 s). ``--from-json`` converts a saved
Overpass response instead of downloading. ``--clip S,W,N,E`` cuts a smaller
box out of an existing extract (how the test fixture was made).

Load the result with :class:`cyberpunk_computer.vision.maps.OsmSpeedMap`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_CENTER = (54.35, 18.65)          # Gdańsk
DEFAULT_SIZE_KM = 15.0
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "cyberpunk-prius-computer/osm_extract (offline speed-limit extract; one query)"
SCALE = 100_000

# Drivable classes. Footways, cycleways, paths, tracks, steps, construction
# and proposed roads are left out on purpose (a car is never on them, and
# they would only attract wrong matches).
DRIVABLE = (
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "service", "road", "busway",
)
KEEP_TAGS = (
    "highway", "maxspeed", "maxspeed:forward", "maxspeed:backward", "maxspeed:type",
    "source:maxspeed", "zone:traffic", "zone:maxspeed", "maxspeed:conditional",
    "name", "ref", "oneway", "junction", "lanes", "motorroad", "dual_carriageway",
    "service",
)
PLACES = ("city", "town", "village", "hamlet", "suburb", "quarter", "neighbourhood")


def bbox_around(lat: float, lon: float, size_km: float) -> Tuple[float, float, float, float]:
    """(S, W, N, E) of a size_km × size_km box centred on (lat, lon)."""
    half = size_km / 2.0
    dlat = half / 111.32
    dlon = half / (111.32 * math.cos(math.radians(lat)))
    return (round(lat - dlat, 5), round(lon - dlon, 5), round(lat + dlat, 5), round(lon + dlon, 5))


def build_query(bbox: Sequence[float], timeout_s: int = 180) -> str:
    s, w, n, e = bbox
    b = f"{s},{w},{n},{e}"
    hw = "|".join(DRIVABLE)
    pl = "|".join(PLACES)
    return (f'[out:json][timeout:{timeout_s}];\n'
            f'(way["highway"~"^({hw})$"]({b});\n'
            f' node["place"~"^({pl})$"]({b}););\n'
            f'out body geom qt;\n')


def fetch(query: str, url: str = OVERPASS_URL, retries: int = 4,
          backoff_s: float = 30.0, sleep=time.sleep, opener=urllib.request.urlopen) -> dict:
    """POST one Overpass query; retry on 429/5xx/timeouts with backoff."""
    data = urllib.parse.urlencode({"data": query}).encode()
    last: Optional[Exception] = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
        try:
            with opener(req, timeout=300) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            last = ex
            if ex.code not in (429, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            last = ex
        if attempt + 1 < retries:
            wait = backoff_s * (2 ** attempt)
            print(f"overpass: {last}; retry in {wait:.0f} s", file=sys.stderr)
            sleep(wait)
    raise RuntimeError(f"overpass failed after {retries} attempts: {last}")


def _q(x: float) -> int:
    return int(round(x * SCALE))


def convert(overpass: dict, bbox: Sequence[float], source: str = "overpass") -> dict:
    """Overpass JSON (``out body geom``) → extract dict (see module doc)."""
    ways: List[Dict] = []
    places: List[Dict] = []
    for el in overpass.get("elements", ()):
        tags = el.get("tags") or {}
        if el.get("type") == "way":
            coords: List[int] = []
            for p in el.get("geometry") or []:
                if p is None:               # vertex outside the box (clipped)
                    continue
                coords += [_q(p["lat"]), _q(p["lon"])]
            if len(coords) < 4 or tags.get("highway") not in DRIVABLE:
                continue
            ways.append({"id": el["id"], "c": coords,
                         "t": {k: tags[k] for k in KEEP_TAGS if k in tags}})
        elif el.get("type") == "node" and tags.get("place") in PLACES:
            places.append({"id": el["id"], "c": [_q(el["lat"]), _q(el["lon"])],
                           "place": tags["place"], "name": tags.get("name", "")})
    ways.sort(key=lambda w: w["id"])
    places.sort(key=lambda p: p["id"])
    return {"format": "osm-speed-extract", "version": 1, "scale": SCALE,
            "bbox": [float(x) for x in bbox],
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": source, "license": "© OpenStreetMap contributors, ODbL 1.0",
            "osm_base": (overpass.get("osm3s") or {}).get("timestamp_osm_base", ""),
            "ways": ways, "places": places}


def clip(extract: dict, bbox: Sequence[float]) -> dict:
    """Ways with at least one vertex inside ``bbox`` (kept whole), places inside."""
    s, w, n, e = (_q(x) for x in bbox)
    sc = extract.get("scale", SCALE)
    if sc != SCALE:
        raise ValueError("clip expects scale 1e5")

    def inside(lat: int, lon: int) -> bool:
        return s <= lat <= n and w <= lon <= e

    ways = [wy for wy in extract["ways"]
            if any(inside(wy["c"][i], wy["c"][i + 1]) for i in range(0, len(wy["c"]), 2))]
    places = [p for p in extract.get("places", ()) if inside(*p["c"])]
    out = dict(extract, bbox=[float(x) for x in bbox], ways=ways, places=places)
    out["source"] = f"{extract.get('source', '')} (clipped)"
    return out


def read(path: str) -> dict:
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def write(extract: dict, path: str) -> int:
    """Write compact JSON (gzipped when ``path`` ends in .gz); returns bytes."""
    raw = json.dumps(extract, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if path.endswith(".gz"):
        raw = gzip.compress(raw, compresslevel=9, mtime=0)
    with open(path, "wb") as f:
        f.write(raw)
    return len(raw)


def _bbox_arg(text: str) -> Tuple[float, ...]:
    b = tuple(float(x) for x in text.split(","))
    if len(b) != 4:
        raise argparse.ArgumentTypeError("needs S,W,N,E")
    return b


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bbox", type=_bbox_arg, help="S,W,N,E in degrees")
    ap.add_argument("--center", default=f"{DEFAULT_CENTER[0]},{DEFAULT_CENTER[1]}",
                    help="LAT,LON of the box centre (default Gdańsk)")
    ap.add_argument("--size-km", type=float, default=DEFAULT_SIZE_KM)
    ap.add_argument("-o", "--out", default="gdansk_15km.json.gz")
    ap.add_argument("--url", default=OVERPASS_URL)
    ap.add_argument("--from-json", help="convert a saved Overpass response instead of downloading")
    ap.add_argument("--save-raw", help="also save the raw Overpass response here")
    ap.add_argument("--clip", type=_bbox_arg, metavar="S,W,N,E",
                    help="cut this box out of the extract given by --in")
    ap.add_argument("--in", dest="inp", help="existing extract (for --clip)")
    args = ap.parse_args(argv)

    if args.clip:
        if not args.inp:
            ap.error("--clip needs --in EXTRACT")
        ext = clip(read(args.inp), args.clip)
    else:
        if args.bbox:
            bbox = args.bbox
        else:
            lat, lon = (float(x) for x in args.center.split(","))
            bbox = bbox_around(lat, lon, args.size_km)
        if args.from_json:
            with open(args.from_json, encoding="utf-8") as f:
                raw = json.load(f)
            source = "overpass (saved response)"
        else:
            print(f"overpass: bbox {bbox} …", file=sys.stderr)
            raw = fetch(build_query(bbox), url=args.url)
            source = f"overpass {args.url}"
            if args.save_raw:
                with open(args.save_raw, "w", encoding="utf-8") as f:
                    json.dump(raw, f)
        ext = convert(raw, bbox, source=source)
    n = write(ext, args.out)
    explicit = sum(1 for w in ext["ways"] if "maxspeed" in w["t"]
                   or "maxspeed:forward" in w["t"] or "maxspeed:backward" in w["t"])
    print(f"{args.out}: {len(ext['ways'])} ways ({explicit} with explicit maxspeed), "
          f"{len(ext['places'])} places, {n / 1024:.0f} KiB", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
