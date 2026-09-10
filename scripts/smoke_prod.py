"""Deployment smoke test for the San Diego GIS MCP server.

Exercises the JSON-RPC surface and the core arcgis tool chain end-to-end
against a running deployment, finishing with the "verification query": a
real parcel lookup at the San Diego City Administration Building (202 C St)
used to confirm the connector. Read-only; paces calls to stay under typical
API Gateway rate limits.

Usage:
    python3 scripts/smoke_prod.py [URL]

URL defaults to a local server (http://localhost:8000/mcp); override with an
argument or the OPENCONTEXT_SMOKE_URL env var to point at a deployment.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

try:  # optional: full schema validation when jsonschema is installed
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    Draft202012Validator = None

URL = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("OPENCONTEXT_SMOKE_URL")
    or "http://localhost:8000/mcp"
)

# San Diego City Administration Building -- public landmark used as the demo.
CITY_HALL_LON, CITY_HALL_LAT = -117.1626, 32.7170

_id = 0
results = []


def rpc(method, params=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read().decode())
    time.sleep(0.4)  # pace under 5 rps
    return body


def raw(method="POST", payload=None, headers=None):
    """Low-level request that returns (status, headers, body) and never
    raises on 4xx/5xx -- the conformance checks assert on those."""
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(URL, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, resp_headers, body = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        status, resp_headers, body = e.code, dict(e.headers), e.read()
    time.sleep(0.4)  # pace under 5 rps
    try:
        parsed = json.loads(body.decode()) if body else None
    except ValueError:
        parsed = body.decode(errors="replace")
    return status, {k.lower(): v for k, v in resp_headers.items()}, parsed


def jsonrpc(method, params=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def call_tool(name, args):
    return rpc("tools/call", {"name": f"arcgis__{name}", "arguments": args})


def text_of(resp):
    return resp["result"]["content"][0]["text"]


def check(label, ok, detail=""):
    results.append(ok)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


print(f"Smoke testing: {URL}\n")

# 1. ping
try:
    r = rpc("ping")
    check("ping", "result" in r and "error" not in r, str(r.get("result")))
except Exception as e:
    check("ping", False, repr(e))

# 2. initialize
try:
    r = rpc(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "1.0"},
        },
    )
    check("initialize", bool(r["result"]["serverInfo"]["name"]))
except Exception as e:
    check("initialize", False, repr(e))

# 3. tools/list -- expect the eight arcgis tools, with the type filter advertised
try:
    r = rpc("tools/list")
    tools = {t["name"]: t for t in r["result"]["tools"]}
    expected = {
        "arcgis__search_datasets",
        "arcgis__get_dataset",
        "arcgis__get_aggregations",
        "arcgis__query_data",
        "arcgis__get_layer_schema",
        "arcgis__get_distinct_values",
        "arcgis__spatial_query_point",
        "arcgis__spatial_query_polygon",
        "arcgis__geocode_address",
    }
    has_all = set(tools) == expected
    type_arg = "type" in (
        tools.get("arcgis__search_datasets", {})
        .get("inputSchema", {})
        .get("properties", {})
    )
    check(
        "tools/list (9 tools + type filter)",
        has_all and type_arg,
        f"{sorted(tools)}",
    )
except Exception as e:
    check("tools/list (9 tools + type filter)", False, repr(e))

# 4. type filter actually restricts results -- SANDAG's catalog mixes Feature
#    Services with Service Definitions, web maps, and apps; type='Feature
#    Service' must return only queryable layers.
try:
    typed = text_of(
        call_tool(
            "search_datasets",
            {"q": "parcels", "type": "Feature Service", "limit": 20},
        )
    )
    ok = (
        "Type: Feature Service" in typed
        and "Type: Service Definition" not in typed
        and "Type: Web Map" not in typed
    )
    check(
        "type filter restricts to Feature Services",
        ok,
        "only Feature Services" if ok else "filter had no effect",
    )
except Exception as e:
    check("type filter restricts to Feature Services", False, repr(e))

# 5. discovery -- find the SanGIS Parcels Feature Service by title
parcels_id = None
try:
    s = text_of(
        call_tool(
            "search_datasets",
            {"q": "parcels", "type": "Feature Service", "limit": 20},
        )
    )
    m = re.search(r"\d+\. Parcels\s*\n\s*ID:\s*(\S+)", s)
    parcels_id = m.group(1) if m else None
    check(
        "search_datasets finds Parcels",
        parcels_id is not None,
        f"id={parcels_id}",
    )
except Exception as e:
    check("search_datasets finds Parcels", False, repr(e))

# 6. get_dataset on the discovered id -- must carry the SanGIS attribution
if parcels_id:
    try:
        t = text_of(call_tool("get_dataset", {"dataset_id": parcels_id}))
        ok = "Parcels" in t and "Attribution:" in t and "SanGIS" in t
        check("get_dataset(Parcels) with attribution", ok, f"{len(t)} chars")
    except Exception as e:
        check("get_dataset(Parcels) with attribution", False, repr(e))

# 7. the layer has queryable records at all (proves layer resolution)
if parcels_id:
    try:
        t = text_of(
            call_tool(
                "query_data", {"dataset_id": parcels_id, "where": "1=1", "limit": 1}
            )
        )
        ok = (
            "TOTAL MATCHING:" in t
            and "Returned" in t
            and "Invalid URL" not in t
            and "failed" not in t
        )
        check("query_data total count (TOTAL MATCHING)", ok, t.split("\n")[0][:60])
    except Exception as e:
        check("query_data total count (TOTAL MATCHING)", False, repr(e))

# 8. VERIFICATION QUERY -- parcels by APN prefix, selected fields. This is the
#    headline end-to-end check: where clause + out_fields against live SanGIS
#    data. Field names on the hosted layers are lowercase.
if parcels_id:
    try:
        t = text_of(
            call_tool(
                "query_data",
                {
                    "dataset_id": parcels_id,
                    "where": "apn LIKE '7602%'",
                    "out_fields": "apn,situs_address,situs_street",
                    "limit": 5,
                },
            )
        )
        has_rows = "Record 1:" in t
        right_shape = "apn:" in t and "situs_street:" in t
        no_error = "Invalid URL" not in t and "failed" not in t
        ok = has_rows and right_shape and no_error
        check(
            "verification query (parcels by APN prefix)",
            ok,
            t.split("\n")[0][:60] if ok else "ERROR/empty: " + t[:80],
        )
    except Exception as e:
        check("verification query (parcels by APN prefix)", False, repr(e))

# 9. get_layer_schema -- list fields for Parcels
if parcels_id:
    try:
        t = text_of(call_tool("get_layer_schema", {"item_id": parcels_id}))
        ok = "Fields (" in t and "apn" in t
        check("get_layer_schema(Parcels)", ok, t.split("\n")[0][:60])
    except Exception as e:
        check("get_layer_schema(Parcels)", False, repr(e))

# 10. get_distinct_values -- jurisdictions recorded on parcels
if parcels_id:
    try:
        t = text_of(
            call_tool(
                "get_distinct_values",
                {"item_id": parcels_id, "field": "situs_juris", "limit": 25},
            )
        )
        ok = "distinct value" in t
        check("get_distinct_values(situs_juris)", ok, t.replace("\n", " ")[:60])
    except Exception as e:
        check("get_distinct_values(situs_juris)", False, repr(e))

# 11. spatial_query_point -- which parcel contains San Diego City Hall?
if parcels_id:
    try:
        t = text_of(
            call_tool(
                "spatial_query_point",
                {
                    "item_id": parcels_id,
                    "lon": CITY_HALL_LON,
                    "lat": CITY_HALL_LAT,
                    "out_fields": "apn,situs_address,situs_street",
                    "limit": 3,
                },
            )
        )
        ok = "Returned" in t and "apn" in t and "Invalid URL" not in t
        check("spatial_query_point(parcel @ City Hall)", ok, t.split("\n")[0][:60])
    except Exception as e:
        check("spatial_query_point(parcel @ City Hall)", False, repr(e))

# 12. geocode_address -- street address to lon/lat (SANDAG composite locator).
try:
    t = text_of(call_tool("geocode_address", {"address": "202 C St, San Diego, CA"}))
    ok = "match(es)" in t and "lon:" in t and "lat:" in t
    check("geocode_address(City Hall)", ok, t.split("\n")[0][:60])
except Exception as e:
    check("geocode_address(City Hall)", False, repr(e))

# 13. spatial_query_point BY ADDRESS -- geocode + point-in-polygon in one call
if parcels_id:
    try:
        t = text_of(
            call_tool(
                "spatial_query_point",
                {
                    "item_id": parcels_id,
                    "address": "202 C St, San Diego, CA",
                    "out_fields": "apn,situs_address,situs_street",
                    "limit": 2,
                },
            )
        )
        ok = "Geocoded" in t and "Returned" in t and "Invalid URL" not in t
        check("spatial_query_point(by address)", ok, t.split("\n")[0][:60])
    except Exception as e:
        check("spatial_query_point(by address)", False, repr(e))

# 13b. spatial_query_polygon -- libraries inside one El Cajon council
#      district, then within a mile of it (server-side buffer must widen the
#      match). Both layers are discovered by title, like Parcels above.
districts_id = libraries_id = None
try:
    s = text_of(
        call_tool(
            "search_datasets",
            {"q": "Council Districts", "type": "Feature Service", "limit": 10},
        )
    )
    m = re.search(r"\d+\. Council_Districts\s*\n\s*ID:\s*(\S+)", s)
    districts_id = m.group(1) if m else None
    s = text_of(
        call_tool(
            "search_datasets", {"q": "Library", "type": "Feature Service", "limit": 10}
        )
    )
    m = re.search(r"\d+\. Library\s*\n\s*ID:\s*(\S+)", s)
    libraries_id = m.group(1) if m else None
    check(
        "search_datasets finds Council_Districts + Library",
        bool(districts_id and libraries_id),
        f"districts={districts_id} libraries={libraries_id}",
    )
except Exception as e:
    check("search_datasets finds Council_Districts + Library", False, repr(e))

POLYGON_ARGS = {
    "filter_where": "jur_name = 'EL CAJON' AND district = 1",
    "out_fields": "name,city",
    "limit": 10,
}
if districts_id and libraries_id:
    try:
        args = {"item_id": libraries_id, "filter_item_id": districts_id, **POLYGON_ARGS}
        t = text_of(call_tool("spatial_query_polygon", args))
        m = re.search(r"TOTAL MATCHING:\s*(\d+)", t)
        exact = int(m.group(1)) if m else None
        check(
            "spatial_query_polygon(libraries in a council district)",
            exact is not None and "name" in t,
            t.split("\n")[0][:60],
        )
        t = text_of(
            call_tool(
                "spatial_query_polygon", {**args, "distance": 1, "units": "miles"}
            )
        )
        m = re.search(r"TOTAL MATCHING:\s*(\d+)", t)
        buffered = int(m.group(1)) if m else None
        check(
            "spatial_query_polygon(1-mile buffer widens the match)",
            exact is not None and buffered is not None and buffered > exact,
            f"exact={exact} within 1 mi={buffered}",
        )
        # All El Cajon districts: a 4-feature union whose raw geometry
        # exceeds the gateway's body cap, so it must be generalised, not 403.
        r = call_tool(
            "spatial_query_polygon",
            {**args, "filter_where": "jur_name = 'EL CAJON'"},
        )
        sc = r["result"].get("structuredContent") or {}
        summ = sc.get("summary", {})
        check(
            "spatial_query_polygon(4-district union fits the gateway cap)",
            summ.get("filter_features") == 4
            and summ.get("filter_simplified_m") is not None
            and (summ.get("total_matching") or 0) >= exact,
            f"filter_features={summ.get('filter_features')} "
            f"simplified_m={summ.get('filter_simplified_m')} "
            f"total={summ.get('total_matching')}",
        )
    except Exception as e:
        check("spatial_query_polygon(libraries in a council district)", False, repr(e))

# 14. get_aggregations sanity
try:
    t = text_of(call_tool("get_aggregations", {"field": "type", "q": "parcels"}))
    check("get_aggregations(type)", "dataset(s)" in t, t.replace("\n", " ")[:60])
except Exception as e:
    check("get_aggregations(type)", False, repr(e))

# ── MCP conformance surface ────────────────────────────────────────────
# Mirrors the checks the sibling forks run after every deploy.

try:
    r = rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        },
    )
    echoed = r["result"]["protocolVersion"] == "2025-06-18"
    instr = r["result"].get("instructions", "")
    info = r["result"]["serverInfo"]
    check(
        "initialize negotiates version + carries instructions",
        echoed and len(instr) > 200 and info["name"] != "opencontext",
        f"echo={echoed} instructions={len(instr)} chars serverInfo={info}",
    )
except Exception as e:
    check("initialize negotiates version + carries instructions", False, repr(e))

try:
    tools_by_name = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
    missing = sorted(
        n
        for n, t in tools_by_name.items()
        if not t.get("title")
        or t.get("annotations", {}).get("readOnlyHint") is not True
        or "idempotentHint" in t.get("annotations", {})
        or not t.get("outputSchema")
    )
    check(
        "tools/list metadata (title + readOnlyHint + outputSchema)",
        not missing,
        "all 8 carry title, readOnlyHint, outputSchema"
        if not missing
        else f"missing: {missing}",
    )
except Exception as e:
    tools_by_name = {}
    check("tools/list metadata (title + readOnlyHint + outputSchema)", False, repr(e))

try:
    err = rpc("resources/list").get("error", {})
    check("unknown method -> -32601", err.get("code") == -32601, str(err)[:70])
except Exception as e:
    check("unknown method -> -32601", False, repr(e))

try:
    err = rpc("tools/call", {"name": "arcgis__nope", "arguments": {}}).get("error", {})
    ok = (
        err.get("code") == -32602
        and err.get("message", "").startswith("Unknown tool")
        and "arcgis__query_data" in json.dumps(err.get("data"))
    )
    check("unknown tool -> -32602 + available tools", ok, str(err)[:70])
except Exception as e:
    check("unknown tool -> -32602 + available tools", False, repr(e))

try:
    err = rpc("tools/call", {"name": "arcgis__get_dataset", "arguments": "x"}).get(
        "error", {}
    )
    check("non-object arguments -> -32602", err.get("code") == -32602, str(err)[:70])
except Exception as e:
    check("non-object arguments -> -32602", False, repr(e))

try:
    r = call_tool("query_data", {"dataset_id": parcels_id or "x", "limit": "many"})
    res = r.get("result", {})
    ok = res.get("isError") is True and "limit must be an integer" in text_of(r)
    check("bad tool argument -> isError with message", ok, text_of(r)[:60])
except Exception as e:
    check("bad tool argument -> isError with message", False, repr(e))

try:
    status, _, body = raw(
        payload=jsonrpc("ping"), headers={"Origin": "https://evil.example"}
    )
    ok = status == 403 and (body or {}).get("error", {}).get("code") == -32600
    check("disallowed Origin -> 403", ok, f"HTTP {status} {str(body)[:50]}")
except Exception as e:
    check("disallowed Origin -> 403", False, repr(e))

try:
    status, hdrs, _ = raw(
        payload=jsonrpc("ping"), headers={"Origin": "https://claude.ai"}
    )
    ok = (
        status == 200 and hdrs.get("access-control-allow-origin") == "https://claude.ai"
    )
    check("allowlisted Origin -> 200 + reflected", ok, f"HTTP {status}")
except Exception as e:
    check("allowlisted Origin -> 200 + reflected", False, repr(e))

try:
    status, _, body = raw(
        payload=jsonrpc("ping"), headers={"MCP-Protocol-Version": "1999-01-01"}
    )
    err = (body or {}).get("error", {})
    ok = (
        status == 400
        and err.get("code") == -32600
        and "2025-11-25" in (err.get("data") or {}).get("supported", [])
    )
    check(
        "bad MCP-Protocol-Version -> 400/-32600", ok, f"HTTP {status} {str(err)[:50]}"
    )
except Exception as e:
    check("bad MCP-Protocol-Version -> 400/-32600", False, repr(e))

try:
    status, _, body = raw(
        payload=jsonrpc("ping"), headers={"MCP-Protocol-Version": "2025-06-18"}
    )
    check(
        "good MCP-Protocol-Version -> 200",
        status == 200 and (body or {}).get("result") == {},
        f"HTTP {status}",
    )
except Exception as e:
    check("good MCP-Protocol-Version -> 200", False, repr(e))

try:
    status, hdrs, _ = raw(method="OPTIONS", headers={"Origin": "https://claude.ai"})
    allowed = hdrs.get("access-control-allow-headers", "").lower()
    ok = (
        status == 200
        and "mcp-protocol-version" in allowed
        and "mcp-session-id" in allowed
    )
    check("OPTIONS preflight allows MCP headers", ok, f"HTTP {status} {allowed[:50]}")
except Exception as e:
    check("OPTIONS preflight allows MCP headers", False, repr(e))

try:
    a = rpc("tools/list")["result"]["tools"]
    b = rpc("tools/list")["result"]["tools"]
    check("tools/list is deterministic", a == b, f"{len(a)} tools")
except Exception as e:
    check("tools/list is deterministic", False, repr(e))


# ── Structured output (outputSchema is BINDING) ───────────────────────
# Validate LIVE structuredContent against the outputSchema the server
# itself advertises, across awkward branches, and assert every structured
# caveat appears verbatim in the prose.


def check_structured(label, tool, args, expect_codes=None):
    try:
        r = call_tool(tool, args)
        res = r["result"]
        sc = res.get("structuredContent")
        schema = tools_by_name.get(f"arcgis__{tool}", {}).get("outputSchema")
        problems = []
        if not sc:
            problems.append("no structuredContent")
        if not schema:
            problems.append("no outputSchema advertised")
        if sc and schema:
            if Draft202012Validator is not None:
                errs = list(Draft202012Validator(schema).iter_errors(sc))
                problems += [f"schema: {e.message}" for e in errs[:3]]
            else:
                missing = [k for k in schema.get("required", []) if k not in sc]
                if missing:
                    problems.append(f"missing keys {missing}")
            text = res["content"][0]["text"]
            for c in sc.get("caveats", []):
                if c["message"] not in text:
                    problems.append(f"caveat {c['code']} absent from prose")
            got = [c["code"] for c in sc.get("caveats", [])]
            for code in expect_codes or []:
                if code not in got:
                    problems.append(f"expected caveat {code}, got {got}")
        detail = (
            "; ".join(problems)
            if problems
            else f"caveats={[c['code'] for c in sc.get('caveats', [])]}"
        )
        check(label, not problems, detail)
    except Exception as e:
        check(label, False, repr(e))


check_structured(
    "structured: search_datasets hit",
    "search_datasets",
    {"q": "parcels", "type": "Feature Service", "limit": 5},
)
check_structured(
    "structured: search_datasets empty",
    "search_datasets",
    {"q": "qwzxjvplk"},
    expect_codes=["no_results"],
)
check_structured("structured: get_aggregations", "get_aggregations", {"field": "type"})
check_structured(
    "structured: geocode_address",
    "geocode_address",
    {"address": "202 C St, San Diego, CA"},
)
if parcels_id:
    check_structured(
        "structured: get_dataset", "get_dataset", {"dataset_id": parcels_id}
    )
    check_structured(
        "structured: query_data truncated",
        "query_data",
        {"dataset_id": parcels_id, "where": "1=1", "limit": 1},
        expect_codes=["results_truncated"],
    )
    check_structured(
        "structured: get_layer_schema",
        "get_layer_schema",
        {"item_id": parcels_id, "keyword": "situs"},
    )
    check_structured(
        "structured: get_distinct_values",
        "get_distinct_values",
        {"item_id": parcels_id, "field": "situs_juris", "limit": 5},
        expect_codes=["results_truncated"],
    )
    check_structured(
        "structured: spatial_query_point by address",
        "spatial_query_point",
        {"item_id": parcels_id, "address": "202 C St, San Diego, CA", "limit": 2},
        expect_codes=["geocoded"],
    )
if districts_id and libraries_id:
    check_structured(
        "structured: spatial_query_polygon buffered by filter layer",
        "spatial_query_polygon",
        {
            "item_id": libraries_id,
            "filter_item_id": districts_id,
            **POLYGON_ARGS,
            "distance": 1,
            "units": "miles",
            "limit": 1,
        },
        expect_codes=["results_truncated"],
    )

print("\n=== SUMMARY ===")
n_pass = sum(results)
print(f"{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
