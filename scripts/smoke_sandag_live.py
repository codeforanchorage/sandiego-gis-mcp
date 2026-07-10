"""Live smoke test for the SANDAG/SanGIS ArcGIS Enterprise endpoints.

Verifies the upstream facts the San Diego GIS MCP plugin relies on,
directly against the live services (no MCP server involved). Read-only.

Checks:
  1. Services directory root reachable anonymously (?f=json)
  2. Hosted/ folder is public and lists FeatureServers
  3. Auth-gated folder (GeoDepot) returns a JSON "Token Required" error
     (HTTP 200 + error body, code 499) -- discovery must skip on this shape
  4. Portal search (sharing/rest/search) works anonymously and returns
     items with service URLs
  5. Hosted/Parcels layer: metadata has copyrightText, maxRecordCount 2000,
     stored SR is EPSG:2230 (latestWkid)
  6. Query with outSR=4326 returns WGS84-range coordinates
  7. Point-in-polygon with inSR=4326 finds the parcel at San Diego City
     Administration Building (202 C St)
  8. SANDAG_COMPOSITE_LOCATOR findAddressCandidates geocodes 202 C St

Usage:
    python3 scripts/smoke_sandag_live.py
"""

import json
import sys
import time
import urllib.parse
import urllib.request

SERVICES_ROOT = "https://geo.sandag.org/server/rest/services"
PORTAL_SEARCH = "https://geo.sandag.org/portal/sharing/rest/search"
PARCELS_LAYER = f"{SERVICES_ROOT}/Hosted/Parcels/FeatureServer/0"
GEOCODER = (
    "https://gis.sandag.org/sdgis/rest/services/"
    "SANDAG_COMPOSITE_LOCATOR/GeocodeServer/findAddressCandidates"
)
# San Diego City Administration Building -- public landmark used as the demo.
CITY_HALL = "202 C St, San Diego, CA"
CITY_HALL_LON, CITY_HALL_LAT = -117.1626, 32.7170

results = []


def get_json(url, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read().decode())
    time.sleep(0.4)  # be polite to the public endpoints
    return body


def check(label, ok, detail=""):
    results.append(ok)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


print(f"Smoke testing SANDAG endpoints (root: {SERVICES_ROOT})\n")

# 1. services directory root
try:
    root = get_json(SERVICES_ROOT, {"f": "json"})
    ok = "folders" in root and "Hosted" in root.get("folders", [])
    check("services directory root (anonymous)", ok, f"folders={root.get('folders')}")
except Exception as e:
    check("services directory root (anonymous)", False, repr(e))

# 2. Hosted folder is public
try:
    hosted = get_json(f"{SERVICES_ROOT}/Hosted", {"f": "json"})
    fs = [s for s in hosted.get("services", []) if s.get("type") == "FeatureServer"]
    ok = "error" not in hosted and len(fs) > 50
    check("Hosted/ folder public", ok, f"{len(fs)} FeatureServers")
except Exception as e:
    check("Hosted/ folder public", False, repr(e))

# 3. auth-gated folder shape -- GeoDepot must answer with a JSON error body
#    (ArcGIS Enterprise returns HTTP 200 + {"error":{"code":499,...}}), which
#    is the shape discovery has to detect and skip.
try:
    gated = get_json(f"{SERVICES_ROOT}/GeoDepot", {"f": "json"})
    err = gated.get("error", {})
    ok = err.get("code") in (499, 498, 403, 401)
    check(
        "auth-gated folder returns token error",
        ok,
        f"code={err.get('code')} message={err.get('message')!r}",
    )
except Exception as e:
    check("auth-gated folder returns token error", False, repr(e))

# 4. portal search anonymous
try:
    s = get_json(
        PORTAL_SEARCH,
        {"q": 'parcels AND type:"Feature Service"', "f": "json", "num": "5"},
    )
    items = s.get("results", [])
    with_url = [i for i in items if (i.get("url") or "").startswith("https://")]
    ok = "error" not in s and s.get("total", 0) > 0 and len(with_url) > 0
    check(
        "portal search anonymous",
        ok,
        f"total={s.get('total')} first={items[0]['title'] if items else None}",
    )
except Exception as e:
    check("portal search anonymous", False, repr(e))

# 5. Parcels layer metadata: attribution + pagination cap + stored SR
try:
    meta = get_json(PARCELS_LAYER, {"f": "json"})
    copyright_text = meta.get("copyrightText") or ""
    sr = meta.get("extent", {}).get("spatialReference", {})
    ok = (
        bool(copyright_text)
        and meta.get("maxRecordCount") == 2000
        and sr.get("latestWkid") == 2230
    )
    check(
        "Parcels layer metadata (copyrightText, maxRecordCount, EPSG:2230)",
        ok,
        f"maxRecordCount={meta.get('maxRecordCount')} "
        f"latestWkid={sr.get('latestWkid')} "
        f"copyright={copyright_text[:40]!r}",
    )
except Exception as e:
    check("Parcels layer metadata", False, repr(e))

# 6. query with outSR=4326 returns WGS84-range coordinates
try:
    q = get_json(
        f"{PARCELS_LAYER}/query",
        {
            "where": "1=1",
            "outFields": "apn",  # hosted-layer field names are lowercase
            "resultRecordCount": "1",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
        },
    )
    feats = q.get("features", [])
    ring_pt = feats[0]["geometry"]["rings"][0][0] if feats else [0, 0]
    ok = (
        "error" not in q
        and feats
        and -180 <= ring_pt[0] <= -110
        and 30 <= ring_pt[1] <= 35
    )
    check("query with outSR=4326 (WGS84 coords)", ok, f"first vertex={ring_pt}")
except Exception as e:
    check("query with outSR=4326 (WGS84 coords)", False, repr(e))

# 7. point-in-polygon with inSR=4326 at the City Administration Building
try:
    q = get_json(
        f"{PARCELS_LAYER}/query",
        {
            "where": "1=1",
            "geometry": f"{CITY_HALL_LON},{CITY_HALL_LAT}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "apn,situs_address,situs_street",
            "returnGeometry": "false",
            "f": "json",
        },
    )
    feats = q.get("features", [])
    ok = "error" not in q and len(feats) >= 1
    detail = feats[0]["attributes"] if feats else q.get("error")
    check("point-in-polygon with inSR=4326 (City Hall parcel)", ok, str(detail)[:80])
except Exception as e:
    check("point-in-polygon with inSR=4326 (City Hall parcel)", False, repr(e))

# 8. composite locator geocode
try:
    g = get_json(
        GEOCODER,
        {"SingleLine": CITY_HALL, "outSR": "4326", "maxLocations": "3", "f": "json"},
    )
    cands = g.get("candidates", [])
    best = cands[0] if cands else {}
    loc = best.get("location", {})
    ok = (
        bool(cands)
        and best.get("score", 0) >= 80
        and abs(loc.get("x", 0) - CITY_HALL_LON) < 0.01
        and abs(loc.get("y", 0) - CITY_HALL_LAT) < 0.01
    )
    check(
        "SANDAG_COMPOSITE_LOCATOR geocode (202 C St)",
        ok,
        f"{best.get('address')!r} -> ({loc.get('x')}, {loc.get('y')})",
    )
except Exception as e:
    check("SANDAG_COMPOSITE_LOCATOR geocode (202 C St)", False, repr(e))

print("\n=== SUMMARY ===")
n_pass = sum(results)
print(f"{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
