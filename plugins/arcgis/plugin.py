"""ArcGIS Enterprise portal plugin implementation for OpenContext.

This plugin provides access to an ArcGIS Enterprise deployment (portal +
server), such as SANDAG/SanGIS's geo.sandag.org. Dataset discovery uses the
Portal REST API search (``/sharing/rest/search``) when it is open to
anonymous callers, and falls back to walking the ArcGIS Server services
directory (``/rest/services``) when it is not. Queries go straight to the
standard Feature Service ``/query`` endpoints.
"""

import asyncio
import html
import json
import logging
import re
import time
import unicodedata
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import httpx
import pyclipper

from core.interfaces import (
    DataPlugin,
    PluginType,
    ToolDefinition,
    ToolInputError,
    ToolResult,
)
from plugins.arcgis.config_schema import ArcGISPluginConfig
from plugins.arcgis.where_validator import WhereValidator

logger = logging.getLogger(__name__)

# US Census oneline geocoder: free, no API key, nationwide, returns WGS84
# lon/lat that feed directly into spatial_query_point. Used as the fallback
# when no ArcGIS GeocodeServer is configured (`geocoder_url`).
_CENSUS_GEOCODER_URL = (
    "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
)

# Portal item IDs are 32-char hex; anything else that looks like
# "Folder/Service/FeatureServer[/N]" is treated as a services-directory path
# (the ID form produced by directory-walk discovery). The path regex is
# deliberately strict -- no scheme, no leading slash, no '..' -- so a dataset
# ID can never be turned into a request to an arbitrary host or path.
_ITEM_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SERVICE_PATH_RE = re.compile(
    r"^[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*/(FeatureServer|MapServer)(?:/\d+)?$"
)

# ArcGIS Enterprise answers requests for auth-gated folders/services with
# HTTP 200 and an error body carrying one of these codes ("Token Required"
# is 499). Directory walking must skip these, not fail on them.
_AUTH_ERROR_CODES = {498, 499, 401, 403}

# HTML-tag stripping and a small unicode->ASCII punctuation map. ArcGIS Hub
# descriptions are authored as HTML and often contain smart quotes, dashes,
# and non-breaking spaces; cleaning these keeps tool output readable and
# ASCII-safe (e.g. for M365 Copilot).
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_UNICODE_PUNCT = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "--",
    "…": "...",
    " ": " ",
    "·": "-",
    "•": "-",
}


# Stable caveat codes. A caller branches on these instead of parsing prose
# that may be reworded. The schema enum below is generated from this tuple,
# so an emitted code outside it is impossible without also changing the
# contract. Kept identical across the GIS forks (some codes are unused here).
CAVEAT_CODES = (
    "limit_clamped",
    "results_truncated",
    "page_cap_reached",
    "pagination_unsupported",
    "count_unavailable",
    "live_metadata",
    "geocoded",
    "multiple_geocode_matches",
    "address_snapped",
    "filter_simplified",
    "directory_mode",
    "no_results",
)


class _Caveats:
    """Warnings for one tool response, rendered into BOTH output forms.

    Every warning is added here exactly once. The prose lines and the
    ``caveats`` array in structuredContent are both derived from this
    list, which is what stops the human-readable text and the
    machine-readable contract from drifting apart as either is edited.
    """

    def __init__(self) -> None:
        self._items: List[Dict[str, str]] = []

    def add(self, code: str, message: Optional[str]) -> None:
        if code not in CAVEAT_CODES:  # pragma: no cover - programming error
            raise RuntimeError(f"unknown caveat code {code!r}")
        if message:
            self._items.append({"code": code, "message": message})

    @property
    def messages(self) -> List[str]:
        return [item["message"] for item in self._items]

    def as_list(self) -> List[Dict[str, str]]:
        return [dict(item) for item in self._items]

    def __len__(self) -> int:
        return len(self._items)


class _PolygonQueryResult(NamedTuple):
    """spatial_query_polygon's answer: rows, best-effort total, and how many
    filter-layer features were unioned (None for an inline geometry)."""

    rows: List[Dict[str, Any]]
    total_matching: Optional[int]
    filter_features: Optional[int]
    simplified_m: Optional[int]


class _ToolOutput(NamedTuple):
    """What a tool handler returns: prose for the model, data for code."""

    text: str
    structured: Dict[str, Any]


# ── Output schemas ────────────────────────────────────────────────────
#
# A declared outputSchema is BINDING -- the spec says servers MUST return
# conforming structured results. These are deliberately loose where the
# real data is loose: rows carry whatever out_fields the caller asked for
# (raw ArcGIS attributes, dates as epoch milliseconds), distinct values can
# be strings, numbers or null, and TOTAL MATCHING is null when the count
# query fails. Shared envelope across all eight tools: {query, summary,
# caveats} plus ONE payload key named for what it carries.

_CAVEATS_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "description": (
        "Warnings about this result. Branch on `code` rather than parsing "
        "the prose; every entry here also appears verbatim in the text "
        "content."
    ),
    "items": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "enum": list(CAVEAT_CODES)},
            "message": {"type": "string"},
        },
        "required": ["code", "message"],
        "additionalProperties": False,
    },
}

_ROW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "One record: RAW layer attributes keyed by physical field name. "
        "Which fields are present depends on out_fields. Date-typed fields "
        "are epoch milliseconds."
    ),
    "additionalProperties": True,
}

_NULLABLE_STR: Dict[str, Any] = {"type": ["string", "null"]}

_DATASET_SUMMARY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "One catalog item (portal item or services-directory service).",
    "properties": {
        "id": {
            "type": "string",
            "description": "32-char portal item id, or a 'Folder/Service/FeatureServer' path.",
        },
        "title": {"type": "string"},
        "type": {
            "type": "string",
            "description": "ArcGIS item type, e.g. 'Feature Service'.",
        },
        "url": {"type": "string"},
        "access": {"type": "string"},
        "owner": {"type": "string"},
        "created": {"type": "string", "description": "ISO date or ''."},
        "modified": {"type": "string", "description": "ISO date or ''."},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "extent": {"type": "array"},
    },
    "required": ["id", "title", "type"],
    "additionalProperties": True,
}


def _envelope_schema(
    description: str,
    query_props: Dict[str, Any],
    summary_props: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Build one tool's output schema around the shared envelope."""
    properties: Dict[str, Any] = {
        "query": {
            "type": "object",
            "description": "What was asked, as the server resolved it.",
            "properties": query_props,
            "additionalProperties": True,
        },
        "summary": {
            "type": "object",
            "description": "Counts and outcome flags for this result.",
            "properties": summary_props,
            "additionalProperties": True,
        },
        "caveats": _CAVEATS_SCHEMA,
    }
    properties.update(payload)
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        # Every declared key is required on every code path; extra keys
        # stay legal so a later addition is not a contract violation.
        "required": ["query", "summary", "caveats", *payload],
        "additionalProperties": True,
    }


_ATTRIBUTION = {
    "type": "string",
    "description": "SanGIS/SANDAG attribution text; must travel with the data.",
}

_OUTPUT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "search_datasets": _envelope_schema(
        "Catalog search results.",
        {"q": {"type": "string"}, "type": _NULLABLE_STR, "limit": {"type": "integer"}},
        {
            "returned": {"type": "integer"},
            "search_mode": {
                "type": "string",
                "enum": ["portal", "directory"],
                "description": "portal = ArcGIS portal search; directory = services-directory walk.",
            },
        },
        {"datasets": {"type": "array", "items": _DATASET_SUMMARY_SCHEMA}},
    ),
    "get_dataset": _envelope_schema(
        "Metadata for one catalog item.",
        {"dataset_id": {"type": "string"}},
        {
            "queryable": {
                "type": "boolean",
                "description": "True when query_data can be used on this id.",
            },
            "attribution": _ATTRIBUTION,
        },
        {
            "dataset": {
                "type": "object",
                "properties": {
                    **_DATASET_SUMMARY_SCHEMA["properties"],
                    "service_url": {"type": "string"},
                    "attribution": {"type": "string"},
                    "geometryType": {"type": "string"},
                    "numRecords": {"type": ["integer", "null"]},
                },
                "required": ["id", "title", "type", "service_url"],
                "additionalProperties": True,
            }
        },
    ),
    "get_aggregations": _envelope_schema(
        "Facet counts over the top matching catalog items.",
        {
            "field": {"type": "string", "enum": ["access", "owner", "tags", "type"]},
            "q": _NULLABLE_STR,
        },
        {"bucket_count": {"type": "integer"}, "items_counted": {"type": "integer"}},
        {
            "buckets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                    "required": ["key", "count"],
                    "additionalProperties": False,
                },
            }
        },
    ),
    "query_data": _envelope_schema(
        "Attribute query results.",
        {
            "dataset_id": {"type": "string"},
            "where": {"type": "string"},
            "out_fields": {"type": "string"},
            "order_by": _NULLABLE_STR,
            "limit": {"type": "integer"},
        },
        {
            "returned": {"type": "integer"},
            "total_matching": {
                "type": ["integer", "null"],
                "description": (
                    "Server-side count of ALL records matching `where`. Null "
                    "when the count query failed -- which is NOT zero."
                ),
            },
            "truncated": {
                "type": "boolean",
                "description": "True when more records match than were returned.",
            },
            "pages_fetched": {"type": "integer"},
            "attribution": _ATTRIBUTION,
        },
        {"rows": {"type": "array", "items": _ROW_SCHEMA}},
    ),
    "get_layer_schema": _envelope_schema(
        "Field list for one layer.",
        {"item_id": {"type": "string"}, "keyword": _NULLABLE_STR},
        {
            "layer_name": {"type": "string"},
            "geometry_type": {"type": "string"},
            "layer_url": {"type": "string"},
            "attribution": _ATTRIBUTION,
            "field_count": {"type": "integer"},
            "filtered": {
                "type": "boolean",
                "description": "True when `keyword` narrowed the list.",
            },
        },
        {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "RAW ArcGIS field descriptor (name, type, alias, domain, ...).",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string"},
                        "alias": {"type": "string"},
                    },
                    "required": ["name"],
                    "additionalProperties": True,
                },
            }
        },
    ),
    "get_distinct_values": _envelope_schema(
        "Distinct values of one field.",
        {
            "item_id": {"type": "string"},
            "field": {"type": "string"},
            "like": _NULLABLE_STR,
            "where": {"type": "string"},
            "limit": {"type": "integer"},
        },
        {
            "returned": {"type": "integer"},
            "truncated": {
                "type": "boolean",
                "description": "True when the list hit `limit`.",
            },
        },
        {
            "values": {
                "type": "array",
                "description": "Raw values in server order; may include null.",
                "items": {},
            }
        },
    ),
    "spatial_query_point": _envelope_schema(
        "Features at (or, when snapped, within a few metres of) a point.",
        {
            "item_id": {"type": "string"},
            "lon": {"type": "number"},
            "lat": {"type": "number"},
            "address": _NULLABLE_STR,
            "matched_address": {
                **_NULLABLE_STR,
                "description": "Geocoder's normalised address when `address` was used.",
            },
            "where": {"type": "string"},
            "out_fields": {"type": "string"},
            "limit": {"type": "integer"},
        },
        {
            "returned": {"type": "integer"},
            "geocoded": {"type": "boolean"},
            "snapped_to_meters": {
                "type": ["integer", "null"],
                "description": (
                    "Set when no feature contained the geocoded point and the "
                    "result is features within this many metres instead."
                ),
            },
            "truncated": {
                "type": "boolean",
                "description": "True when the result hit `limit`.",
            },
            "attribution": _ATTRIBUTION,
        },
        {"rows": {"type": "array", "items": _ROW_SCHEMA}},
    ),
    "spatial_query_polygon": _envelope_schema(
        "Features that spatially relate to a polygon filter, optionally buffered.",
        {
            "item_id": {"type": "string"},
            "filter_item_id": _NULLABLE_STR,
            "filter_where": {
                **_NULLABLE_STR,
                "description": "WHERE applied to `filter_item_id`; null for an inline geometry.",
            },
            "filter_geometry_type": {
                **_NULLABLE_STR,
                "description": (
                    "GeoJSON type of the inline filter (Polygon, MultiPolygon, "
                    "Feature); null when a filter layer was used."
                ),
            },
            "spatial_rel": {"type": "string"},
            "distance": {
                "type": ["number", "null"],
                "description": "Server-side buffer applied to the filter; null when unbuffered.",
            },
            "units": _NULLABLE_STR,
            "where": {"type": "string"},
            "out_fields": {"type": "string"},
            "limit": {"type": "integer"},
        },
        {
            "returned": {"type": "integer"},
            "total_matching": {
                "type": ["integer", "null"],
                "description": (
                    "Server-side count of ALL features matching the filter. Null "
                    "when the count query failed -- which is NOT zero."
                ),
            },
            "filter_features": {
                "type": ["integer", "null"],
                "description": (
                    "How many filter-layer features were unioned into the filter "
                    "polygon; null for an inline `filter_geometry`."
                ),
            },
            "filter_simplified_m": {
                "type": ["integer", "null"],
                "description": (
                    "Tolerance in metres the filter polygon was generalised to "
                    "so it fits the gateway's body cap; null when sent as-is."
                ),
            },
            "truncated": {
                "type": "boolean",
                "description": "True when more features match than were returned.",
            },
            "attribution": _ATTRIBUTION,
        },
        {"rows": {"type": "array", "items": _ROW_SCHEMA}},
    ),
    "geocode_address": _envelope_schema(
        "Geocoder candidates.",
        {"address": {"type": "string"}},
        {
            "returned": {"type": "integer"},
            "geocoder": {
                "type": "string",
                "enum": ["arcgis", "census"],
                "description": "Which geocoder answered: the configured ArcGIS locator or the US Census fallback.",
            },
        },
        {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "matched_address": {"type": "string"},
                        "lon": {"type": "number"},
                        "lat": {"type": "number"},
                    },
                    "required": ["matched_address", "lon", "lat"],
                    "additionalProperties": True,
                },
            }
        },
    ),
}


class ArcGISPlugin(DataPlugin):
    """Plugin for accessing an ArcGIS Enterprise portal and its services.

    This plugin implements the DataPlugin interface and provides tools for
    searching the portal catalog (with a services-directory fallback),
    retrieving dataset metadata, querying Feature Services, and exploring
    catalog aggregations.
    """

    plugin_name = "arcgis"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "2.0.0"

    QUERYABLE_TYPES = {
        "Feature Layer",
        "Feature Service",
        "Map Service",
        "Table",
    }

    # Fields get_aggregations can tally. Counts are computed client-side
    # from the top search results because the portal's countFields API
    # returns counts only intermittently on this deployment.
    AGGREGATABLE_FIELDS = ("access", "owner", "tags", "type")

    # get_dataset results are cached briefly: a single query_data tool call
    # resolves the same dataset up to three times (query, count, attribution).
    _DATASET_CACHE_TTL = 300.0
    _DATASET_CACHE_MAX = 128

    # The services directory changes rarely; cache the walk so each search
    # doesn't re-fetch every folder.
    _DIRECTORY_CACHE_TTL = 300.0
    # The directory listing carries nothing but service names, so each
    # queryable service is described from its own metadata (description,
    # copyrightText, layer names) in one fan-out under a wall-clock budget.
    # Measured 2026-09-09: 382 services at ~60 ms each is ~2 s at 16-wide;
    # serial would be ~24 s, past the 20 s plugin timeout. A service whose
    # fetch fails or misses the budget simply stays name-only.
    _DIRECTORY_ENRICH_CONCURRENCY = 16
    _DIRECTORY_ENRICH_BUDGET_S = 8.0
    _DIRECTORY_ENRICH_REQUEST_TIMEOUT_S = 5.0

    # Retry radius for address-form spatial_query_point when the geocoded
    # point hits nothing. Verified against SANDAG parcels at City Hall: 10 m
    # recovers the named parcel from a street-centerline geocode, 20 m
    # already pulls in unrelated lots across the block.
    _ADDRESS_SNAP_METERS = 10

    # spatial_query_polygon: spatial relations and linear units accepted from
    # the model, and their ArcGIS REST spellings.
    _SPATIAL_REL_MAP = {
        "intersects": "esriSpatialRelIntersects",
        "contains": "esriSpatialRelContains",
        "within": "esriSpatialRelWithin",
        "crosses": "esriSpatialRelCrosses",
        "touches": "esriSpatialRelTouches",
        "overlaps": "esriSpatialRelOverlaps",
        "envelope_intersects": "esriSpatialRelEnvelopeIntersects",
    }
    _LINEAR_UNIT_ALIASES = {
        "meters": "meters", "meter": "meters", "metre": "meters",
        "metres": "meters", "m": "meters",
        "kilometers": "kilometers", "kilometer": "kilometers",
        "kilometre": "kilometers", "kilometres": "kilometers",
        "km": "kilometers",
        "feet": "feet", "foot": "feet", "ft": "feet",
        "miles": "miles", "mile": "miles", "mi": "miles",
        "yards": "yards", "yard": "yards", "yd": "yards",
    }  # fmt: skip
    _ESRI_LINEAR_UNITS = {
        "meters": "esriSRUnit_Meter",
        "kilometers": "esriSRUnit_Kilometer",
        "feet": "esriSRUnit_Foot",
        "miles": "esriSRUnit_StatuteMile",
        "yards": "esriSRUnit_Yard",
    }

    # Caps on inbound filter polygons. ArcGIS accepts far larger geometries,
    # but they become huge POST bodies and slow spatial plans. A SANDAG
    # council district is ~2,400 coordinates; these leave ample headroom.
    MAX_FILTER_RINGS = 1000
    MAX_FILTER_COORDS = 10000
    # SANDAG's gateway (Azure Application Gateway WAF) answers a bare 403 to
    # request bodies over ~128 KB: measured 2026-09-09, a 96 KB body passes
    # and 144 KB is refused. Filter geometry is serialised to 6 decimals
    # (~0.1 m) and, when still over this budget, generalised with pyclipper
    # at the smallest tolerance in the ladder that fits; the response
    # carries a `filter_simplified` caveat naming the tolerance. The 4-ring
    # El Cajon council-district union (155 KB) fits at 1 m with the same
    # library count as the per-district queries.
    MAX_FILTER_BYTES = 90_000
    _SIMPLIFY_LADDER_M = (1, 2, 5, 10, 20, 50)
    _METERS_PER_DEGREE = 111_000.0
    # Cap on filter-layer features fetched and unioned into one filter
    # polygon. Above this, refuse LOUDLY rather than silently union a
    # truncated subset. Sized for the 20 s plugin timeout: 500 polygons
    # with geometry is one page from a MaxRecordCount=2000 service.
    MAX_FILTER_FEATURES = 500
    FILTER_FETCH_PAGE = 500
    # Integer scaling for the degree-space pyclipper union of filter rings:
    # 1 clipper unit = 1e-7 degree (~1 cm).
    _UNION_DEG_SCALE = 1e7

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[ArcGISPluginConfig] = None
        self.portal_client: Optional[httpx.AsyncClient] = None
        self.feature_client: Optional[httpx.AsyncClient] = None
        # "portal" (sharing/rest/search) or "directory" (services walk).
        self._search_mode: str = "portal"
        self._dataset_cache: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = (
            OrderedDict()
        )
        self._directory_cache: List[Dict[str, Any]] = []
        self._directory_cache_expiry: float = 0.0

    async def initialize(self) -> bool:
        try:
            self.plugin_config = ArcGISPluginConfig(**self.config)

            headers = {"Accept": "application/json"}
            feature_headers = {}
            if self.plugin_config.token:
                headers["Authorization"] = f"Bearer {self.plugin_config.token}"
                feature_headers["Authorization"] = f"Bearer {self.plugin_config.token}"

            if self.plugin_config.portal_url:
                self.portal_client = httpx.AsyncClient(
                    base_url=self.plugin_config.portal_url,
                    headers=headers,
                    timeout=self.plugin_config.timeout,
                )

            self.feature_client = httpx.AsyncClient(
                headers=feature_headers,
                timeout=self.plugin_config.timeout,
            )

            self._search_mode = await self._probe_search_mode()

            self._initialized = True
            logger.info(
                f"ArcGIS plugin initialized for {self.plugin_config.city_name} "
                f"(search mode: {self._search_mode})"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to initialize ArcGIS plugin: {e}", exc_info=True)
            return False

    async def _probe_search_mode(self) -> str:
        """Pick the discovery mechanism: anonymous portal search if it
        answers, otherwise a services-directory walk. Raises if neither
        endpoint is usable."""
        if self.portal_client:
            try:
                response = await self.portal_client.get(
                    "/sharing/rest/search",
                    params={"q": 'type:"Feature Service"', "num": 1, "f": "json"},
                )
                response.raise_for_status()
                data = response.json()
                if "error" not in data:
                    return "portal"
                logger.warning(
                    f"Portal search not anonymous "
                    f"({data['error'].get('message')}); falling back to "
                    f"services directory"
                )
            except Exception as e:
                logger.warning(
                    f"Portal search probe failed ({e}); falling back to "
                    f"services directory"
                )
        if not self.plugin_config.services_url:
            raise RuntimeError(
                "Portal search is unavailable and no services_url is "
                "configured for directory-walk discovery"
            )
        response = await self.feature_client.get(
            self.plugin_config.services_url, params={"f": "json"}
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(
                f"Services directory error: {data['error'].get('message')}"
            )
        return "directory"

    async def shutdown(self) -> None:
        if self.portal_client:
            await self.portal_client.aclose()
            self.portal_client = None
        if self.feature_client:
            await self.feature_client.aclose()
            self.feature_client = None
        self._initialized = False
        logger.info("ArcGIS plugin shut down")

    # Human-readable display names. The wire `name` is prefixed
    # (`arcgis__query_data`) because it must be a stable, collision-free
    # identifier; that string reads poorly in a client's tool picker.
    # Clients resolve display names as title -> annotations.title -> name.
    # Keyed by the UNPREFIXED tool name.
    TOOL_TITLES = {
        "search_datasets": "Search Datasets",
        "get_dataset": "Dataset Details",
        "get_aggregations": "Catalog Facets",
        "query_data": "Query Data",
        "get_layer_schema": "Layer Schema",
        "get_distinct_values": "Distinct Values",
        "spatial_query_point": "What's at This Point",
        "spatial_query_polygon": "Query by Area or Buffer",
        "geocode_address": "Geocode Address",
    }

    # Every tool here is a read-only query against a public, external
    # ArcGIS service: safe to call without confirmation, and results may
    # change between calls as SanGIS republishes layers.
    TOOL_ANNOTATIONS = {"readOnlyHint": True, "openWorldHint": True}

    def get_tools(self) -> List[ToolDefinition]:
        tools = self._tool_definitions()
        for tool in tools:
            tool.title = self.TOOL_TITLES.get(tool.name)
            tool.annotations = dict(self.TOOL_ANNOTATIONS)
            tool.output_schema = _OUTPUT_SCHEMAS[tool.name]
        return tools

    def _tool_definitions(self) -> List[ToolDefinition]:
        city = self.plugin_config.city_name if self.plugin_config else "Unknown"
        scope_note = (self.plugin_config.scope_note if self.plugin_config else "") or ""
        search_description = (
            f"Search {city}'s ArcGIS portal catalog. To find datasets "
            "you can actually query or map, set type='Feature Service' "
            "-- other item types (web maps, apps, service definitions) "
            "are not directly queryable. Each result shows its item "
            "type and dataset ID; pass that ID to get_dataset or "
            "query_data."
        )
        if scope_note:
            search_description += f" {scope_note}"
        return [
            ToolDefinition(
                name="search_datasets",
                description=search_description,
                input_schema={
                    "type": "object",
                    "properties": {
                        "q": {
                            "type": "string",
                            "description": "Full-text search query",
                        },
                        "type": {
                            "type": "string",
                            "description": (
                                "Optional: restrict results to one ArcGIS item type. "
                                "Use 'Feature Service' for queryable spatial/tabular "
                                "layers (the analyzable data). Other common values: "
                                "'Map Service', 'Web Map', 'Web Mapping Application'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                    "required": ["q"],
                },
            ),
            ToolDefinition(
                name="get_dataset",
                description="Get metadata for a specific dataset by ID",
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": (
                                "Dataset ID from search_datasets: a 32-char hex "
                                "portal item ID, or a services-directory path "
                                "like 'Hosted/Parcels/FeatureServer'"
                            ),
                        },
                    },
                    "required": ["dataset_id"],
                },
            ),
            ToolDefinition(
                name="get_aggregations",
                description=(
                    "Get facet counts for a field across the portal catalog "
                    "(tallied over the top matching items). Useful for "
                    "exploring available types, tags, or owners."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "description": (
                                "Field to aggregate. Available fields: "
                                '"type", "tags", "access", "owner"'
                            ),
                        },
                        "q": {
                            "type": "string",
                            "description": "Optional search query to scope the aggregation",
                        },
                    },
                    "required": ["field"],
                },
            ),
            ToolDefinition(
                name="query_data",
                description=(
                    "Query records from an ArcGIS Feature Service by dataset "
                    "ID (the plugin resolves the service URL automatically). The "
                    "output leads with TOTAL MATCHING, the full count of records "
                    "matching `where` -- so for 'how many X?' you do not need to "
                    "page through results. Use `order_by` (e.g. 'Date_Submitted "
                    "DESC') for most-recent / top-N questions, and "
                    "get_layer_schema first for CASE-SENSITIVE field names."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": "Dataset ID (same as get_dataset)",
                        },
                        "where": {
                            "type": "string",
                            "description": "SQL WHERE clause for filtering",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": "Comma-separated field names to return",
                            "default": "*",
                        },
                        "order_by": {
                            "type": "string",
                            "description": (
                                "Optional ORDER BY, e.g. 'Date_Submitted DESC' "
                                "for most-recent-first. Field names are "
                                "CASE-SENSITIVE."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of records (default: 100)",
                            "default": 100,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["dataset_id"],
                },
            ),
            ToolDefinition(
                name="get_layer_schema",
                description=(
                    "List a dataset's fields (name, type, alias, coded values) "
                    "so you can write a correct query_data WHERE clause without "
                    "guessing. Field names are CASE-SENSITIVE. Pass a dataset "
                    "ID; optional `keyword` shows only matching fields. Typical "
                    "chain: search_datasets -> get_layer_schema -> query_data."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "Dataset ID of a Feature Service / Table.",
                        },
                        "keyword": {
                            "type": "string",
                            "description": (
                                "Optional: only show fields whose name or alias "
                                "contains this term."
                            ),
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="get_distinct_values",
                description=(
                    "List the distinct values in one field of a dataset -- to "
                    "confirm the exact spelling/format of codes before filtering "
                    "(e.g. 'Residential' vs '1 or 2 Family Dwelling'). Field "
                    "names are CASE-SENSITIVE (use get_layer_schema first). "
                    "Optional `like` substring-narrows the values."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "Dataset ID of a Feature Service / Table.",
                        },
                        "field": {
                            "type": "string",
                            "description": (
                                "Field name (CASE-SENSITIVE) to list values for."
                            ),
                        },
                        "like": {
                            "type": "string",
                            "description": (
                                "Optional substring; only values containing it "
                                "are returned."
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": (
                                "Optional WHERE clause to narrow contributing records."
                            ),
                            "default": "1=1",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max distinct values (default 200).",
                            "default": 200,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["item_id", "field"],
                },
            ),
            ToolDefinition(
                name="spatial_query_point",
                description=(
                    "Point-in-polygon lookup: return the attributes of every "
                    "polygon in a dataset that contains a point -- 'which "
                    "parcel / flood zone / district is at this location?'. "
                    "Provide EITHER a street `address` (geocoded automatically) "
                    "OR both `lon` and `lat` (WGS84). Coordinates in and out "
                    "are always WGS84 / EPSG:4326 (inSR=4326 is declared and "
                    "outSR=4326 requested; the server converts from the "
                    "layers' native spatial reference). Use on polygon Feature "
                    "Services (check geometry with get_layer_schema). Returns "
                    "attributes only, no geometry."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "Dataset ID of a polygon Feature Service.",
                        },
                        "address": {
                            "type": "string",
                            "description": (
                                "Street address to geocode (alternative to "
                                "lon/lat), e.g. '202 C St' (City Hall). "
                                "Resolved by the configured geocoder."
                            ),
                        },
                        "lon": {
                            "type": "number",
                            "description": (
                                "Longitude, WGS84 decimal degrees (-180 to 180). "
                                "Note: lon first. Omit if `address` is given."
                            ),
                        },
                        "lat": {
                            "type": "number",
                            "description": (
                                "Latitude, WGS84 decimal degrees (-90 to 90). "
                                "Omit if `address` is given."
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": "Optional WHERE clause to further filter.",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": "Comma-separated field names to return.",
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max features (default 10, max 50).",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="spatial_query_polygon",
                description=(
                    "Server-side spatial selection: return the features of a "
                    "dataset that intersect (or otherwise relate to) a polygon. "
                    "The polygon is EITHER inline GeoJSON (`filter_geometry`, "
                    "WGS84) OR feature(s) of another polygon layer "
                    "(`filter_item_id` + `filter_where`, e.g. one council "
                    "district; all matching features are unioned). The target "
                    "layer can be polygon, line, or point. PROXIMITY ('within N "
                    "miles/feet of X'): set `distance` + `units` to buffer the "
                    "filter polygon server-side -- e.g. libraries within 1 mile "
                    "of a district = item_id=<libraries>, "
                    'filter_item_id=<districts>, filter_where="district = 1", '
                    "distance=1, units='miles'. Call get_layer_schema on BOTH "
                    "layers first: they have different fields. Output leads "
                    "with TOTAL MATCHING, so 'how many X in Y?' needs no "
                    "paging. Returns attributes only, no geometry."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "Dataset ID of the TARGET layer (the features "
                                "to select). Polygon, line, or point."
                            ),
                        },
                        "filter_item_id": {
                            "type": "string",
                            "description": (
                                "Dataset ID of a POLYGON layer whose feature(s) "
                                "form the filter. Combine with filter_where to "
                                "pick specific features. Prefer this over "
                                "filter_geometry when the boundary is already "
                                "published (districts, jurisdictions, zones)."
                            ),
                        },
                        "filter_where": {
                            "type": "string",
                            "description": (
                                "WHERE clause on filter_item_id selecting the "
                                "filter feature(s), e.g. \"jur_name = 'EL CAJON'\". "
                                "All matches are unioned into one polygon."
                            ),
                            "default": "1=1",
                        },
                        "filter_geometry": {
                            "type": "object",
                            "description": (
                                "Inline GeoJSON Polygon, MultiPolygon, or a "
                                "Feature wrapping one, in WGS84 (lon, lat). "
                                "Alternative to filter_item_id."
                            ),
                        },
                        "spatial_rel": {
                            "type": "string",
                            "enum": [
                                "intersects",
                                "contains",
                                "within",
                                "crosses",
                                "touches",
                                "overlaps",
                                "envelope_intersects",
                            ],
                            "description": (
                                "Spatial relation of target to filter: "
                                "'intersects' (any overlap, default), 'contains' "
                                "(filter contains target), 'within' (target "
                                "within filter), 'crosses', 'touches', "
                                "'overlaps', 'envelope_intersects'."
                            ),
                            "default": "intersects",
                        },
                        "distance": {
                            "type": "number",
                            "minimum": 0,
                            "description": (
                                "Buffer the filter polygon by this distance "
                                "(in `units`) before testing spatial_rel. Use "
                                "for 'within N of' questions. Omit or 0 for an "
                                "exact overlap."
                            ),
                        },
                        "units": {
                            "type": "string",
                            "enum": ["meters", "kilometers", "feet", "miles", "yards"],
                            "description": "Linear unit for `distance` (default meters).",
                            "default": "meters",
                        },
                        "where": {
                            "type": "string",
                            "description": "Optional WHERE clause on the target layer.",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": "Comma-separated target field names to return.",
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max features (default 25, max 1000).",
                            "default": 25,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="geocode_address",
                description=(
                    "Convert a street address to coordinates (lon/lat, WGS84) via "
                    "the configured ArcGIS geocoder (or the US Census geocoder "
                    "as fallback). Use the result with spatial_query_point, or "
                    "call spatial_query_point with `address` directly. Include "
                    "city/state to disambiguate -- the geocoder covers the "
                    "whole region, not a single city."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "string",
                            "description": "Street address, e.g. '202 C St' (City Hall).",
                        },
                    },
                    "required": ["address"],
                },
            ),
        ]

    @staticmethod
    def _int_arg(arguments: Dict[str, Any], name: str, default: int) -> int:
        """Read an integer argument, rejecting garbage as a caller error.

        A bare ``int()`` over caller input raises a stdlib ValueError that
        logs as a server fault and tells the caller nothing useful.
        """
        raw = arguments.get(name, default)
        if raw is None:
            return default
        if isinstance(raw, bool):
            raise ToolInputError(f"{name} must be an integer (got {raw!r})")
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ToolInputError(f"{name} must be an integer (got {raw!r})") from None

    @staticmethod
    def _float_arg(arguments: Dict[str, Any], name: str) -> Optional[float]:
        """Read an optional float argument, rejecting garbage as a caller error."""
        raw = arguments.get(name)
        if raw is None:
            return None
        if isinstance(raw, bool):
            raise ToolInputError(f"{name} must be a number (got {raw!r})")
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise ToolInputError(f"{name} must be a number (got {raw!r})") from None

    @staticmethod
    def _require_str(arguments: Dict[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not value or not isinstance(value, str):
            raise ToolInputError(f"{name} is required")
        return value

    @staticmethod
    def _clamp_limit(limit: int, ceiling: int, caveats: "_Caveats") -> int:
        """Enforce a tool's server-side ceiling, recording it as a caveat
        rather than silently returning fewer rows than asked for."""
        if limit < 1:
            raise ToolInputError(f"limit must be at least 1 (got {limit})")
        if limit > ceiling:
            caveats.add(
                "limit_clamped",
                f"limit {limit} was clamped to this tool's maximum of {ceiling}.",
            )
            return ceiling
        return limit

    @staticmethod
    def _envelope(
        query: Dict[str, Any],
        summary: Dict[str, Any],
        caveats: "_Caveats",
        **payload: Any,
    ) -> Dict[str, Any]:
        """Assemble the {query, summary, caveats, <payload>} envelope."""
        envelope: Dict[str, Any] = {
            "query": query,
            "summary": summary,
            "caveats": caveats.as_list(),
        }
        envelope.update(payload)
        return envelope

    @staticmethod
    def _with_caveats(text: str, caveats: "_Caveats") -> str:
        """Render every caveat into the prose, on EVERY return path, so
        anything in structured `caveats` is also visible to a model that
        only reads the text."""
        if not len(caveats):
            return text
        return text.rstrip("\n") + "\n\n" + "\n".join(caveats.messages)

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        handler = getattr(self, f"_tool_{tool_name}", None)
        if tool_name not in self.TOOL_TITLES or handler is None:
            return ToolResult(
                content=[],
                success=False,
                error_message=f"Unknown tool: {tool_name}",
            )
        try:
            output: _ToolOutput = await handler(arguments)
            return ToolResult(
                content=[{"type": "text", "text": output.text}],
                structured_content=output.structured,
                success=True,
            )
        except ToolInputError as e:
            # The caller passed something invalid. WARNING, no traceback:
            # a stack trace here is noise that buries real faults, and the
            # message alone already tells the caller how to fix the call.
            logger.warning(f"Invalid arguments for tool {tool_name}: {e}")
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Invalid tool arguments",
            )
        except Exception as e:
            # Everything else IS a server or upstream fault -- keep the
            # traceback, that is what these logs are for.
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )

    # ── Tool handlers: each returns prose + structured content ──────────

    @staticmethod
    def _dataset_summary(d: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(d.get("id", "")),
            "title": str(d.get("title", "") or ""),
            "type": str(d.get("type", "") or ""),
            "url": str(d.get("url", "") or ""),
            "access": str(d.get("access", "") or ""),
            "owner": str(d.get("owner", "") or ""),
            "created": str(d.get("created", "") or ""),
            "modified": str(d.get("modified", "") or ""),
            "description": str(d.get("description", "") or ""),
            "tags": [str(t) for t in (d.get("tags") or [])],
            "extent": list(d.get("extent") or []),
        }

    async def _tool_search_datasets(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        q = a.get("q") or ""
        item_type = a.get("type") or None
        limit = self._clamp_limit(self._int_arg(a, "limit", 10), 100, caveats)
        datasets = await self.search_datasets(q, limit, item_type)
        if not datasets:
            caveats.add(
                "no_results",
                "No datasets found. Try a broader keyword, or set "
                "type='Feature Service' to see queryable layers only.",
            )
        structured = self._envelope(
            {"q": q, "type": item_type, "limit": limit},
            {
                "returned": len(datasets),
                "search_mode": self._search_mode or "portal",
            },
            caveats,
            datasets=[self._dataset_summary(d) for d in datasets],
        )
        text = self._with_caveats(self._format_search_results(datasets), caveats)
        return _ToolOutput(text, structured)

    async def _tool_get_dataset(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        dataset_id = self._require_str(a, "dataset_id")
        dataset = await self.get_dataset(dataset_id)
        ds_type = str(dataset.get("type", "") or "")
        service_url = str(dataset.get("service_url", "") or "")
        queryable = bool(service_url) and (
            not ds_type or ds_type in self.QUERYABLE_TYPES
        )
        payload = {**self._dataset_summary(dataset), **dataset}
        payload["service_url"] = service_url
        payload["attribution"] = str(dataset.get("attribution", "") or "")
        structured = self._envelope(
            {"dataset_id": dataset_id},
            {"queryable": queryable, "attribution": payload["attribution"]},
            caveats,
            dataset=payload,
        )
        text = self._with_caveats(self._format_dataset(dataset), caveats)
        return _ToolOutput(text, structured)

    async def _tool_get_aggregations(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        field = self._require_str(a, "field")
        q = a.get("q") or None
        buckets = await self.get_aggregations(field, q)
        if self._search_mode == "directory" and field in ("tags", "owner"):
            caveats.add(
                "directory_mode",
                f"Portal catalog search is unavailable, so discovery is "
                f"running from the services directory, which records no "
                f"{field}; only 'type' facets are meaningful in this mode.",
            )
        if not buckets:
            caveats.add(
                "no_results",
                f"No aggregation results for '{field}'"
                + (f" with query '{q}'" if q else "")
                + ".",
            )
        out = [
            {"key": str(b.get("key", "")), "count": int(b.get("doc_count", 0))}
            for b in buckets
        ]
        structured = self._envelope(
            {"field": field, "q": q},
            {"bucket_count": len(out), "items_counted": sum(b["count"] for b in out)},
            caveats,
            buckets=out,
        )
        text = self._with_caveats(self._format_aggregations(field, buckets), caveats)
        return _ToolOutput(text, structured)

    async def _tool_query_data(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        dataset_id = self._require_str(a, "dataset_id")
        where = a.get("where") or "1=1"
        out_fields = a.get("out_fields") or "*"
        order_by = a.get("order_by") or None
        limit = self._clamp_limit(self._int_arg(a, "limit", 100), 1000, caveats)
        filters: Dict[str, Any] = {"where": where, "out_fields": out_fields}
        if order_by:
            filters["order_by"] = order_by
        records, meta = await self._fetch_records(dataset_id, filters, limit)
        # Total match count is best-effort: a count failure must not hide
        # the records we already fetched.
        try:
            total: Optional[int] = await self.get_record_count(dataset_id, where)
        except Exception as count_err:
            logger.warning(f"Could not get record count: {count_err}")
            total = None
            caveats.add(
                "count_unavailable",
                "The total match count is unavailable: the count query "
                "failed, so the total is unknown (not zero).",
            )
        attribution = await self._attribution_for(dataset_id)
        truncated = (total is not None and total > len(records)) or (
            total is None and meta["exceeded_transfer_limit"] and len(records) >= limit
        )
        if truncated:
            caveats.add(
                "results_truncated",
                f"Only the first {len(records)} matching record(s) are shown"
                + (f" of {total}" if total is not None else "")
                + f" (limit {limit}); raise limit or narrow `where`.",
            )
        if not records:
            caveats.add(
                "no_results",
                "No records matched. Check field names with get_layer_schema "
                "and exact values with get_distinct_values.",
            )
        structured = self._envelope(
            {
                "dataset_id": dataset_id,
                "where": where,
                "out_fields": out_fields,
                "order_by": order_by,
                "limit": limit,
            },
            {
                "returned": len(records),
                "total_matching": total,
                "truncated": bool(truncated),
                "pages_fetched": meta["pages"],
                "attribution": attribution,
            },
            caveats,
            rows=records,
        )
        text = self._with_caveats(
            self._format_query_results(
                records, limit, total=total, attribution=attribution
            ),
            caveats,
        )
        return _ToolOutput(text, structured)

    async def _tool_get_layer_schema(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        item_id = self._require_str(a, "item_id")
        keyword = a.get("keyword") or None
        schema = await self.get_layer_schema(item_id, keyword)
        fields = schema.get("fields", []) or []
        if not fields:
            caveats.add(
                "no_results",
                "No fields found for this layer"
                + (f" matching '{keyword}'" if keyword else "")
                + ".",
            )
        structured = self._envelope(
            {"item_id": item_id, "keyword": keyword},
            {
                "layer_name": schema.get("layer_name", "") or "",
                "geometry_type": schema.get("geometry_type", "") or "",
                "layer_url": schema.get("layer_url", "") or "",
                "attribution": schema.get("copyright", "") or "",
                "field_count": len(fields),
                "filtered": bool(keyword),
            },
            caveats,
            fields=[f for f in fields if isinstance(f, dict) and f.get("name")],
        )
        text = self._with_caveats(self._format_layer_schema(schema), caveats)
        return _ToolOutput(text, structured)

    async def _tool_get_distinct_values(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        item_id = self._require_str(a, "item_id")
        field = self._require_str(a, "field")
        like = a.get("like") or None
        where = a.get("where") or "1=1"
        limit = self._clamp_limit(self._int_arg(a, "limit", 200), 1000, caveats)
        values = await self.get_distinct_values(item_id, field, like, where, limit)
        truncated = len(values) >= limit
        if truncated:
            caveats.add(
                "results_truncated",
                f"Distinct values were capped at {limit}; more may exist. Pass "
                "a `like` filter or raise limit.",
            )
        if not values:
            caveats.add("no_results", f"No distinct values found for '{field}'.")
        structured = self._envelope(
            {
                "item_id": item_id,
                "field": field,
                "like": like,
                "where": where,
                "limit": limit,
            },
            {"returned": len(values), "truncated": truncated},
            caveats,
            values=list(values),
        )
        text = self._with_caveats(self._format_distinct_values(field, values), caveats)
        return _ToolOutput(text, structured)

    async def _tool_spatial_query_point(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        item_id = self._require_str(a, "item_id")
        lon = self._float_arg(a, "lon")
        lat = self._float_arg(a, "lat")
        address = a.get("address") or None
        matched_address: Optional[str] = None
        if (lon is None or lat is None) and address:
            candidates = await self.geocode_address(address)
            if not candidates:
                raise ToolInputError(
                    f"Could not geocode address: {address}. Try including the "
                    f"city and state."
                )
            lon = candidates[0]["lon"]
            lat = candidates[0]["lat"]
            matched_address = candidates[0]["matched_address"]
            caveats.add(
                "geocoded",
                f"Geocoded '{address}' -> {matched_address} ({lat}, {lon})",
            )
            if len(candidates) > 1:
                caveats.add(
                    "multiple_geocode_matches",
                    f"{len(candidates)} geocode matches for '{address}'; the "
                    "first was used. Call geocode_address to see them all.",
                )
        if lon is None or lat is None:
            raise ToolInputError("Provide either `address` or both `lon` and `lat`.")
        where = a.get("where") or "1=1"
        out_fields = a.get("out_fields") or "*"
        limit = self._clamp_limit(self._int_arg(a, "limit", 10), 50, caveats)
        records = await self.spatial_query_point(
            item_id, lon, lat, where, out_fields, limit
        )
        snapped: Optional[int] = None
        if not records and matched_address is not None:
            # Geocoders place addresses on the street centerline, so the
            # point can fall in the right-of-way just outside the parcel it
            # names. Retry once within a few meters; the caveat keeps the
            # caller honest about what was matched.
            records = await self.spatial_query_point(
                item_id,
                lon,
                lat,
                where,
                out_fields,
                limit,
                distance_m=self._ADDRESS_SNAP_METERS,
            )
            if records:
                snapped = self._ADDRESS_SNAP_METERS
                caveats.add(
                    "address_snapped",
                    f"No feature contains the geocoded point exactly; showing "
                    f"features within {snapped} m of it (geocoders place "
                    f"addresses on the street centerline).",
                )
        attribution = await self._attribution_for(item_id)
        truncated = len(records) >= limit
        if truncated:
            caveats.add(
                "results_truncated",
                f"Result hit the limit of {limit}; more features may match this point.",
            )
        if not records:
            caveats.add(
                "no_results",
                "No feature in this layer contains the point. Check that "
                "item_id is a polygon layer (get_dataset) and that lon/lat are "
                "WGS84 with lon first.",
            )
        structured = self._envelope(
            {
                "item_id": item_id,
                "lon": lon,
                "lat": lat,
                "address": address,
                "matched_address": matched_address,
                "where": where,
                "out_fields": out_fields,
                "limit": limit,
            },
            {
                "returned": len(records),
                "geocoded": matched_address is not None,
                "snapped_to_meters": snapped,
                "truncated": truncated,
                "attribution": attribution,
            },
            caveats,
            rows=records,
        )
        text = self._with_caveats(
            self._format_query_results(records, limit, attribution=attribution),
            caveats,
        )
        return _ToolOutput(text, structured)

    async def _tool_spatial_query_polygon(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        item_id = self._require_str(a, "item_id")
        filter_item_id = a.get("filter_item_id") or None
        filter_where = a.get("filter_where") or "1=1"
        filter_geometry = a.get("filter_geometry")
        if filter_geometry is not None and not isinstance(filter_geometry, dict):
            raise ToolInputError(
                f"filter_geometry must be a GeoJSON object "
                f"(got {type(filter_geometry).__name__})"
            )
        spatial_rel = str(a.get("spatial_rel") or "intersects").lower()
        distance = self._float_arg(a, "distance")
        if distance is not None and distance < 0:
            raise ToolInputError(f"distance must be >= 0 (got {distance})")
        buffered = bool(distance)
        units = self._normalize_linear_unit(a.get("units")) if buffered else None
        where = a.get("where") or "1=1"
        out_fields = a.get("out_fields") or "*"
        limit = self._clamp_limit(self._int_arg(a, "limit", 25), 1000, caveats)
        result = await self.spatial_query_polygon(
            item_id,
            filter_geometry=filter_geometry,
            filter_item_id=filter_item_id,
            filter_where=filter_where,
            spatial_rel=spatial_rel,
            where=where,
            out_fields=out_fields,
            limit=limit,
            distance=distance if buffered else None,
            units=units or "meters",
        )
        records, total = result.rows, result.total_matching
        if result.simplified_m is not None:
            caveats.add(
                "filter_simplified",
                f"The filter polygon exceeded the SANDAG gateway's request "
                f"body cap and was generalised to a {result.simplified_m} m "
                f"tolerance before the query; features within "
                f"{result.simplified_m} m of the filter boundary may be "
                f"included or missed.",
            )
        if total is None:
            caveats.add(
                "count_unavailable",
                "The total match count is unavailable: the count query "
                "failed, so the total is unknown (not zero).",
            )
        attribution = await self._attribution_for(item_id)
        truncated = (total is not None and total > len(records)) or (
            total is None and len(records) >= limit
        )
        if truncated:
            caveats.add(
                "results_truncated",
                f"Only the first {len(records)} matching feature(s) are shown"
                + (f" of {total}" if total is not None else "")
                + f" (limit {limit}); raise limit or narrow `where`.",
            )
        if not records:
            caveats.add(
                "no_results",
                "No feature in the target layer matched the filter polygon. "
                "Check `where` against get_layer_schema, and that filter_where "
                "selects the intended feature(s) of the filter layer.",
            )
        structured = self._envelope(
            {
                "item_id": item_id,
                "filter_item_id": filter_item_id,
                "filter_where": filter_where if filter_item_id else None,
                "filter_geometry_type": (
                    str(filter_geometry.get("type", "")) if filter_geometry else None
                ),
                "spatial_rel": spatial_rel,
                "distance": distance if buffered else None,
                "units": units,
                "where": where,
                "out_fields": out_fields,
                "limit": limit,
            },
            {
                "returned": len(records),
                "total_matching": total,
                "filter_features": result.filter_features,
                "filter_simplified_m": result.simplified_m,
                "truncated": bool(truncated),
                "attribution": attribution,
            },
            caveats,
            rows=records,
        )
        text = self._with_caveats(
            self._format_query_results(
                records, limit, total=total, attribution=attribution
            ),
            caveats,
        )
        return _ToolOutput(text, structured)

    async def _tool_geocode_address(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        address = self._require_str(a, "address")
        candidates = await self.geocode_address(address)
        if not candidates:
            region = (
                self.plugin_config.geocoder_region if self.plugin_config else ""
            ) or "City, ST"
            caveats.add(
                "no_results",
                f"No geocode match for '{address}'. Try including the city and "
                f"state, e.g. '{address}, {region}'.",
            )
        geocoder = (
            "arcgis"
            if self.plugin_config and self.plugin_config.geocoder_url
            else "census"
        )
        structured = self._envelope(
            {"address": address},
            {"returned": len(candidates), "geocoder": geocoder},
            caveats,
            candidates=[
                {
                    **c,
                    "matched_address": str(c.get("matched_address", "")),
                    "lon": float(c["lon"]),
                    "lat": float(c["lat"]),
                }
                for c in candidates
            ],
        )
        text = self._with_caveats(self._format_geocode(address, candidates), caveats)
        return _ToolOutput(text, structured)

    # ── DataPlugin abstract method implementations ──────────────────────

    async def search_datasets(
        self, query: str, limit: int = 10, item_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        results = await self._search_items(query, limit, item_type)
        # Multi-word queries can over-constrain and return nothing; retry once
        # with the single most distinctive (longest) word rather than give up.
        if not results and query and len(query.split()) > 1:
            longest = max(query.split(), key=len)
            results = await self._search_items(longest, limit, item_type)
        return results

    async def _search_items(
        self, query: str, limit: int, item_type: Optional[str]
    ) -> List[Dict[str, Any]]:
        if self._search_mode == "directory":
            return await self._search_directory(query, limit, item_type)
        return await self._search_portal(query, limit, item_type)

    async def _search_portal(
        self, query: str, limit: int, item_type: Optional[str]
    ) -> List[Dict[str, Any]]:
        clauses = []
        if query:
            clauses.append(query)
        if item_type:
            # Portal search syntax; strip double quotes to keep the phrase valid.
            safe_type = item_type.replace('"', "")
            clauses.append(f'type:"{safe_type}"')
        params: Dict[str, Any] = {
            "q": " AND ".join(clauses) if clauses else "access:public",
            "num": limit,
            "f": "json",
        }
        try:
            response = await self.portal_client.get(
                "/sharing/rest/search",
                params=params,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Portal search error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e

        data = response.json()
        if "error" in data:
            raise RuntimeError(
                f"Portal search error: "
                f"{data['error'].get('message', str(data['error']))}"
            )
        return [self._extract_dataset_summary(item) for item in data.get("results", [])]

    # Directory-walk fallback: map ArcGIS item types to server endpoint types.
    _TYPE_TO_SERVER = {
        "feature service": "FeatureServer",
        "map service": "MapServer",
    }
    _SERVER_TO_TYPE = {v: k.title() for k, v in _TYPE_TO_SERVER.items()}

    async def _list_services_directory(self) -> List[Dict[str, Any]]:
        """Walk the services directory (root + one folder level), skipping
        auth-gated folders, and cache the result briefly."""
        now = time.monotonic()
        if self._directory_cache and now < self._directory_cache_expiry:
            return self._directory_cache

        base = self.plugin_config.services_url
        response = await self.feature_client.get(base, params={"f": "json"})
        response.raise_for_status()
        root = response.json()
        if "error" in root:
            raise RuntimeError(
                f"Services directory error: {root['error'].get('message')}"
            )

        services = list(root.get("services", []))
        for folder in root.get("folders", []):
            try:
                fresp = await self.feature_client.get(
                    f"{base}/{folder}", params={"f": "json"}
                )
                fresp.raise_for_status()
                fdata = fresp.json()
            except Exception as e:
                logger.warning(f"Skipping services folder {folder}: {e}")
                continue
            err = fdata.get("error")
            if err:
                # Auth-gated folders (e.g. Digital_Infrastructure) answer HTTP 200 with a
                # "Token Required" error body -- skip them, don't fail.
                level = (
                    logger.info
                    if err.get("code") in _AUTH_ERROR_CODES
                    else logger.warning
                )
                level(f"Skipping services folder {folder}: {err.get('message')}")
                continue
            services.extend(fdata.get("services", []))

        await self._enrich_directory_services(services)
        self._directory_cache = services
        self._directory_cache_expiry = now + self._DIRECTORY_CACHE_TTL
        return services

    async def _enrich_directory_services(self, services: List[Dict[str, Any]]) -> None:
        """Attach description, attribution and layer names to each queryable
        service from its own metadata, in parallel under a wall-clock budget.
        Nothing here can fail the walk: a fetch that errors or does not
        finish in time leaves that service name-only."""
        targets = [s for s in services if s.get("type") in self._SERVER_TO_TYPE]
        if not targets:
            return
        base = self.plugin_config.services_url
        semaphore = asyncio.Semaphore(self._DIRECTORY_ENRICH_CONCURRENCY)

        async def describe(svc: Dict[str, Any]) -> None:
            async with semaphore:
                response = await self.feature_client.get(
                    f"{base}/{svc['name']}/{svc['type']}",
                    params={"f": "json"},
                    timeout=self._DIRECTORY_ENRICH_REQUEST_TIMEOUT_S,
                )
                response.raise_for_status()
                meta = response.json()
            if "error" in meta:
                return
            description = self._clean_text(
                meta.get("serviceDescription") or meta.get("description") or ""
            )
            if len(description) > 300:
                description = description[:300] + "..."
            svc["description"] = description
            svc["attribution"] = self._clean_text(meta.get("copyrightText") or "")
            svc["layers"] = [
                str(layer.get("name"))
                for layer in (meta.get("layers") or [])
                if layer.get("name")
            ]

        tasks = [asyncio.create_task(describe(s)) for s in targets]
        done, pending = await asyncio.wait(
            tasks, timeout=self._DIRECTORY_ENRICH_BUDGET_S
        )
        for task in pending:
            task.cancel()
        failed = sum(1 for t in done if t.exception() is not None)
        if failed or pending:
            logger.warning(
                f"Directory enrichment described {len(done) - failed} of "
                f"{len(targets)} services: {failed} failed, {len(pending)} "
                f"unfinished within {self._DIRECTORY_ENRICH_BUDGET_S:.0f}s"
            )

    async def _search_directory(
        self, query: str, limit: int, item_type: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Substring-match services in the services directory: every term
        must appear in the service name, or failing that in its description
        or layer names (see _enrich_directory_services). Name matches rank
        first. Dataset IDs in this mode are service paths like
        'Hosted/Parcels/FeatureServer'."""
        server_types = set(self._TYPE_TO_SERVER.values())
        if item_type:
            wanted = self._TYPE_TO_SERVER.get(item_type.lower())
            if not wanted:
                return []  # directory only serves Feature/Map Services
            server_types = {wanted}

        terms = [t for t in query.lower().split() if t]
        name_hits: List[Dict[str, Any]] = []
        text_hits: List[Dict[str, Any]] = []
        for svc in await self._list_services_directory():
            if svc.get("type") not in server_types:
                continue
            name_text = svc.get("name", "").lower().replace("_", " ")
            full_text = (
                " ".join(
                    [name_text, svc.get("description", ""), *svc.get("layers", [])]
                )
                .lower()
                .replace("_", " ")
            )
            if not terms or all(t in name_text for t in terms):
                name_hits.append(svc)
            elif all(t in full_text for t in terms):
                text_hits.append(svc)
        return [self._directory_result(svc) for svc in (name_hits + text_hits)[:limit]]

    def _directory_result(self, svc: Dict[str, Any]) -> Dict[str, Any]:
        name = svc.get("name", "")  # e.g. "Hosted/Parcels"
        path = f"{name}/{svc['type']}"
        return {
            "id": path,
            "title": name.rsplit("/", 1)[-1].replace("_", " "),
            "description": svc.get("description", ""),
            "type": self._SERVER_TO_TYPE[svc["type"]],
            "url": f"{self.plugin_config.services_url}/{path}",
            "access": "public",
            "owner": "",
            "created": "",
            "modified": "",
            "tags": [],
            "extent": [],
        }

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        now = time.monotonic()
        cached = self._dataset_cache.get(dataset_id)
        if cached and now < cached[0]:
            self._dataset_cache.move_to_end(dataset_id)
            return cached[1]

        if _ITEM_ID_RE.match(dataset_id):
            result = await self._get_portal_item(dataset_id)
        elif _SERVICE_PATH_RE.match(dataset_id) and ".." not in dataset_id:
            result = await self._get_directory_service(dataset_id)
        else:
            raise ToolInputError(
                f"Invalid dataset ID {dataset_id!r}: expected a 32-char hex "
                f"portal item ID or a service path like "
                f"'Hosted/Parcels/FeatureServer'"
            )

        self._dataset_cache[dataset_id] = (now + self._DATASET_CACHE_TTL, result)
        self._dataset_cache.move_to_end(dataset_id)
        while len(self._dataset_cache) > self._DATASET_CACHE_MAX:
            self._dataset_cache.popitem(last=False)
        return result

    async def _get_portal_item(self, dataset_id: str) -> Dict[str, Any]:
        if not self.portal_client:
            raise ToolInputError(
                f"Dataset ID {dataset_id!r} is a portal item ID but no "
                f"portal_url is configured"
            )
        try:
            response = await self.portal_client.get(
                f"/sharing/rest/content/items/{dataset_id}",
                params={"f": "json"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Portal item error (HTTP {e.response.status_code}): {e.response.text}"
            ) from e

        item = response.json()
        if "error" in item:
            raise RuntimeError(
                f"Portal item error: {item['error'].get('message', str(item['error']))}"
            )

        result = self._extract_dataset_summary(item)
        license_info = self._clean_text(item.get("licenseInfo", ""))
        if len(license_info) > 300:
            # SanGIS items carry the full multi-page EULA here; the link in
            # the README covers it, a 300-char excerpt is enough in-band.
            license_info = license_info[:300] + "..."
        result.update(
            {
                "snippet": self._clean_text(item.get("snippet", "")),
                "licenseInfo": license_info,
                "spatialReference": item.get("spatialReference", ""),
                "geometryType": item.get("geometryType", ""),
                "additionalResources": [],
                "numRecords": None,
                "service_url": item.get("url", ""),
                # accessInformation is the portal's credits/attribution field
                # (SanGIS attribution) -- passed through in tool responses.
                "attribution": self._clean_text(item.get("accessInformation", "")),
            }
        )
        return result

    async def _get_directory_service(self, dataset_id: str) -> Dict[str, Any]:
        service_url = f"{self.plugin_config.services_url}/{dataset_id}"
        try:
            response = await self.feature_client.get(service_url, params={"f": "json"})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Service metadata error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e

        meta = response.json()
        if "error" in meta:
            raise RuntimeError(
                f"Service metadata error: "
                f"{meta['error'].get('message', str(meta['error']))}"
            )

        server_type = dataset_id.rstrip("0123456789/").rsplit("/", 1)[-1]
        name = dataset_id.split("/" + server_type)[0].rsplit("/", 1)[-1]
        description = self._clean_text(
            meta.get("serviceDescription") or meta.get("description") or ""
        )
        if len(description) > 300:
            description = description[:300] + "..."
        return {
            "id": dataset_id,
            "title": name.replace("_", " "),
            "description": description,
            "type": self._SERVER_TO_TYPE.get(server_type, server_type),
            "url": service_url,
            "access": "public",
            "owner": "",
            "created": "",
            "modified": "",
            "tags": [],
            "extent": [],
            "snippet": "",
            "licenseInfo": "",
            "spatialReference": "",
            "geometryType": "",
            "additionalResources": [],
            "numRecords": None,
            "service_url": service_url,
            "attribution": self._clean_text(meta.get("copyrightText", "")),
        }

    async def query_data(
        self,
        resource_id: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        records, _ = await self._fetch_records(resource_id, filters, limit)
        return records

    async def _fetch_records(
        self,
        resource_id: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """query_data plus the paging facts the structured output reports:
        pages fetched and whether the server flagged exceededTransferLimit
        on the last page."""
        if limit < 1:
            raise ToolInputError(f"limit must be at least 1 (got {limit})")
        dataset = await self.get_dataset(resource_id)
        service_url = dataset.get("service_url")
        ds_type = dataset.get("type", "")
        if not service_url:
            raise ToolInputError(
                f"Dataset {resource_id} does not have a queryable Feature Service URL"
            )

        if ds_type and ds_type not in self.QUERYABLE_TYPES:
            raise ToolInputError(
                f"Dataset type '{ds_type}' is not queryable. "
                f"query_data only supports: {', '.join(sorted(self.QUERYABLE_TYPES))}."
            )

        where_clause = filters.get("where", "1=1") if filters else "1=1"
        where_clause = WhereValidator.validate(where_clause)
        out_fields = filters.get("out_fields", "*") if filters else "*"
        order_by = filters.get("order_by") if filters else None

        service_url = await self._ensure_layer_url(service_url)
        query_url = f"{service_url}/query"
        record_count = min(limit, 1000)
        base_params = {
            "where": where_clause,
            "outFields": out_fields,
            # Layers are stored in a local SR (EPSG:2230 for SANDAG); pin
            # the output to WGS84 for any geometry the server includes.
            "outSR": 4326,
            "f": "json",
            "returnGeometry": "false",
        }
        if order_by:
            base_params["orderByFields"] = order_by

        # Page with resultOffset: a layer's MaxRecordCount can be smaller
        # than the requested count, in which case the server truncates the
        # page and sets exceededTransferLimit.
        records: List[Dict[str, Any]] = []
        meta: Dict[str, Any] = {"pages": 0, "exceeded_transfer_limit": False}
        offset = 0
        while len(records) < record_count:
            params = dict(base_params)
            params["resultRecordCount"] = record_count - len(records)
            if offset:
                params["resultOffset"] = offset

            try:
                response = await self.feature_client.get(query_url, params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise RuntimeError(
                    f"Feature Service query error (HTTP {e.response.status_code}): "
                    f"{e.response.text}"
                ) from e

            try:
                data = response.json()
            except Exception as json_err:
                content_type = response.headers.get("content-type", "")
                raise ValueError(
                    f"Feature Service returned non-JSON response "
                    f"(content-type: {content_type}). The dataset URL may not "
                    f"point to a queryable ArcGIS Feature Service."
                ) from json_err

            error_in_body = data.get("error")
            if error_in_body:
                code = error_in_body.get("code", "unknown")
                msg = error_in_body.get("message", "Unknown error")
                details = error_in_body.get("details", [])
                detail_str = "; ".join(details) if details else ""
                raise RuntimeError(
                    f"Feature Service query failed (code {code}): {msg}"
                    + (f" — {detail_str}" if detail_str else "")
                )

            meta["pages"] += 1
            meta["exceeded_transfer_limit"] = bool(data.get("exceededTransferLimit"))
            features = data.get("features", [])
            records.extend(f.get("attributes", {}) for f in features)
            if not features or not data.get("exceededTransferLimit"):
                break
            offset += len(features)

        return records, meta

    # ── Aggregations (standalone helper, not a DataPlugin method) ───────

    async def get_aggregations(
        self, field: str, q: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Tally facet counts for `field` over the top matching items.

        Computed client-side from search results: the portal's countFields
        parameter returns counts only intermittently on this deployment,
        and the services-directory fallback has no counts API at all.
        """
        if field not in self.AGGREGATABLE_FIELDS:
            raise ToolInputError(
                f"'{field}' is not an aggregatable field. Available fields: "
                f"{', '.join(self.AGGREGATABLE_FIELDS)}."
            )
        # 100 is the portal search page cap.
        items = await self._search_items(q or "", 100, None)
        counter: Counter = Counter()
        for item in items:
            value = item.get(field)
            if isinstance(value, list):
                counter.update(str(v) for v in value if v)
            elif value:
                counter[str(value)] += 1
        return [{"key": k, "doc_count": n} for k, n in counter.most_common()]

    # ── Schema / distinct values / spatial point ────────────────────────

    async def _attribution_for(self, dataset_id: str) -> str:
        """Best-effort attribution text for a dataset (SanGIS credits).

        Served from the dataset cache in the common case; never lets an
        attribution lookup failure break a tool response that already has
        its data.
        """
        try:
            dataset = await self.get_dataset(dataset_id)
            return dataset.get("attribution", "") or ""
        except Exception as e:
            logger.warning(f"Could not get attribution for {dataset_id}: {e}")
            return ""

    async def _layer_url_for_item(self, item_id: str) -> str:
        """Resolve a Hub item ID to a concrete queryable layer URL."""
        dataset = await self.get_dataset(item_id)
        service_url = dataset.get("service_url")
        if not service_url:
            raise ToolInputError(
                f"Dataset {item_id} does not have a queryable Feature Service URL"
            )
        return await self._ensure_layer_url(service_url)

    async def _query_layer(
        self, layer_url: str, params: Dict[str, Any], post: bool = False
    ) -> Dict[str, Any]:
        """Run an ArcGIS Feature Service /query and return parsed JSON, raising
        on HTTP errors or error objects embedded in the response body.

        `post` sends the params as a form body: filter polygons routinely
        exceed URL length limits, a point never does."""
        query_url = f"{layer_url}/query"
        try:
            if post:
                response = await self.feature_client.post(query_url, data=params)
            else:
                response = await self.feature_client.get(query_url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service query error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e
        data = response.json()
        err = data.get("error")
        if err:
            code = err.get("code", "unknown")
            msg = err.get("message", "Unknown error")
            details = "; ".join(err.get("details", []) or [])
            raise RuntimeError(
                f"Feature Service query failed (code {code}): {msg}"
                + (f" -- {details}" if details else "")
            )
        return data

    async def get_record_count(self, item_id: str, where: str = "1=1") -> int:
        """Total number of records matching `where` (returnCountOnly)."""
        layer_url = await self._layer_url_for_item(item_id)
        where_clause = WhereValidator.validate(where)
        data = await self._query_layer(
            layer_url,
            {"where": where_clause, "returnCountOnly": "true", "f": "json"},
        )
        return int(data.get("count", 0))

    async def get_layer_schema(
        self, item_id: str, keyword: Optional[str] = None
    ) -> Dict[str, Any]:
        layer_url = await self._layer_url_for_item(item_id)
        try:
            response = await self.feature_client.get(layer_url, params={"f": "json"})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service metadata error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e
        meta = response.json()
        err = meta.get("error")
        if err:
            raise RuntimeError(
                f"Could not read layer schema (code {err.get('code', 'unknown')}): "
                f"{err.get('message', 'Unknown error')}"
            )
        fields = meta.get("fields", []) or []
        if keyword:
            kw = keyword.lower()
            fields = [
                f
                for f in fields
                if kw in (f.get("name", "") or "").lower()
                or kw in (f.get("alias", "") or "").lower()
            ]
        return {
            "layer_name": meta.get("name", ""),
            "geometry_type": meta.get("geometryType", ""),
            "layer_url": layer_url,
            "copyright": self._clean_text(meta.get("copyrightText", "")),
            "fields": fields,
        }

    async def get_distinct_values(
        self,
        item_id: str,
        field: str,
        like: Optional[str] = None,
        where: str = "1=1",
        limit: int = 200,
    ) -> List[Any]:
        layer_url = await self._layer_url_for_item(item_id)
        where_clause = WhereValidator.validate(where)
        if like:
            safe_like = like.replace("'", "''")
            like_clause = f"{field} LIKE '%{safe_like}%'"
            where_clause = (
                like_clause
                if where_clause in ("", "1=1")
                else f"({where_clause}) AND {like_clause}"
            )
        params = {
            "where": where_clause,
            "outFields": field,
            "returnDistinctValues": "true",
            "returnGeometry": "false",
            "outSR": 4326,
            "orderByFields": field,
            "resultRecordCount": min(max(limit, 1), 1000),
            "f": "json",
        }
        data = await self._query_layer(layer_url, params)
        values = []
        for feat in data.get("features", []):
            attrs = feat.get("attributes", {})
            if field in attrs:
                values.append(attrs[field])
        return values

    async def spatial_query_point(
        self,
        item_id: str,
        lon: float,
        lat: float,
        where: str = "1=1",
        out_fields: str = "*",
        limit: int = 10,
        distance_m: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Features intersecting a WGS84 point. With `distance_m`, features
        within that many meters of the point instead (server-side buffer)."""
        if not -180 <= lon <= 180:
            raise ToolInputError(f"lon must be between -180 and 180 (got {lon})")
        if not -90 <= lat <= 90:
            raise ToolInputError(f"lat must be between -90 and 90 (got {lat})")
        layer_url = await self._layer_url_for_item(item_id)
        where_clause = WhereValidator.validate(where)
        params = {
            "where": where_clause,
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": out_fields,
            "returnGeometry": "false",
            "outSR": 4326,
            "resultRecordCount": min(max(limit, 1), 50),
            "f": "json",
        }
        if distance_m:
            params["distance"] = distance_m
            params["units"] = "esriSRUnit_Meter"
        data = await self._query_layer(layer_url, params)
        return [f.get("attributes", {}) for f in data.get("features", [])]

    async def spatial_query_polygon(
        self,
        item_id: str,
        filter_geometry: Optional[Dict[str, Any]] = None,
        filter_item_id: Optional[str] = None,
        filter_where: str = "1=1",
        spatial_rel: str = "intersects",
        where: str = "1=1",
        out_fields: str = "*",
        limit: int = 25,
        distance: Optional[float] = None,
        units: str = "meters",
    ) -> _PolygonQueryResult:
        """Features of `item_id` that spatially relate to a polygon filter.

        The filter is inline GeoJSON (`filter_geometry`) or the union of the
        features `filter_where` selects in `filter_item_id`. A positive
        `distance` buffers the filter server-side before `spatial_rel` is
        tested, which is how "within N miles of X" is answered. The total
        count is best-effort: None (never zero) when the count query fails.
        """
        if not filter_geometry and not filter_item_id:
            raise ToolInputError(
                "Provide either `filter_geometry` (inline GeoJSON polygon) or "
                "`filter_item_id` (+ optional `filter_where`)."
            )
        spatial_rel_esri = self._SPATIAL_REL_MAP.get((spatial_rel or "").lower())
        if not spatial_rel_esri:
            raise ToolInputError(
                f"spatial_rel must be one of {sorted(self._SPATIAL_REL_MAP)} "
                f"(got {spatial_rel!r})"
            )
        esri_units: Optional[str] = None
        if distance is not None:
            if distance < 0:
                raise ToolInputError(f"distance must be >= 0 (got {distance})")
            if distance > 0:
                esri_units = self._ESRI_LINEAR_UNITS[self._normalize_linear_unit(units)]

        filter_features: Optional[int] = None
        if filter_geometry:
            esri_filter = self._geojson_to_esri_polygon(filter_geometry)
        else:
            esri_filter, filter_features = await self._fetch_filter_polygon(
                filter_item_id or "", filter_where
            )

        rings, simplified_m = self._fit_filter_to_budget(esri_filter["rings"])

        layer_url = await self._layer_url_for_item(item_id)
        where_clause = WhereValidator.validate(where)
        params: Dict[str, Any] = {
            "where": where_clause,
            "geometry": self._serialize_rings(rings),
            "geometryType": "esriGeometryPolygon",
            "inSR": 4326,
            "spatialRel": spatial_rel_esri,
            "outFields": out_fields,
            "returnGeometry": "false",
            "outSR": 4326,
            "resultRecordCount": min(max(limit, 1), 1000),
            "f": "json",
        }
        if esri_units:
            params["distance"] = distance
            params["units"] = esri_units
        data = await self._query_layer(layer_url, params, post=True)
        rows = [f.get("attributes", {}) for f in data.get("features", [])]

        # Total match count is best-effort: a count failure must not hide
        # the rows we already fetched.
        count_params = {
            k: v
            for k, v in params.items()
            if k not in ("resultRecordCount", "outFields")
        }
        count_params["returnCountOnly"] = "true"
        try:
            count_data = await self._query_layer(layer_url, count_params, post=True)
            total: Optional[int] = int(count_data["count"])
        except Exception as count_err:
            logger.warning(f"Could not count polygon-query matches: {count_err}")
            total = None
        return _PolygonQueryResult(rows, total, filter_features, simplified_m)

    @staticmethod
    def _serialize_rings(rings: List[Any]) -> str:
        """Compact Esri polygon JSON, coordinates rounded to 6 decimals
        (~0.1 m): the server's 15-digit floats are a third of the body."""
        rounded = [
            [[round(pt[0], 6), round(pt[1], 6)] for pt in ring] for ring in rings
        ]
        return json.dumps(
            {"rings": rounded, "spatialReference": {"wkid": 4326}},
            separators=(",", ":"),
        )

    @classmethod
    def _fit_filter_to_budget(cls, rings: List[Any]) -> Tuple[List[Any], Optional[int]]:
        """Generalise the filter rings until they fit MAX_FILTER_BYTES.

        Returns the rings to send and the tolerance in metres they were
        cleaned to, or None when they were small enough as-is. Refuses when
        even the coarsest tolerance does not fit.
        """
        if len(cls._serialize_rings(rings)) <= cls.MAX_FILTER_BYTES:
            return rings, None
        paths = cls._rings_to_paths(rings)
        for tol_m in cls._SIMPLIFY_LADDER_M:
            tol = int(round(tol_m / cls._METERS_PER_DEGREE * cls._UNION_DEG_SCALE))
            cleaned = cls._paths_to_rings(pyclipper.CleanPolygons(paths, tol))
            if cleaned and len(cls._serialize_rings(cleaned)) <= cls.MAX_FILTER_BYTES:
                return cleaned, tol_m
        raise ToolInputError(
            f"The filter polygon is too large to send even after generalising "
            f"it to {cls._SIMPLIFY_LADDER_M[-1]} m. Narrow filter_where to "
            f"fewer features, or pass a simpler filter_geometry."
        )

    @classmethod
    def _normalize_linear_unit(cls, units: Optional[str]) -> str:
        """Canonicalize a free-text linear unit (default meters)."""
        key = (units or "meters").strip().lower()
        canonical = cls._LINEAR_UNIT_ALIASES.get(key)
        if canonical is None:
            raise ToolInputError(
                f"units {units!r} is not a supported linear unit. Use one of: "
                f"meters, kilometers, feet, miles, yards."
            )
        return canonical

    @classmethod
    def _geojson_to_esri_polygon(cls, geojson: Any) -> Dict[str, Any]:
        """GeoJSON Polygon / MultiPolygon / Feature -> Esri polygon JSON (WGS84)."""
        if not isinstance(geojson, dict):
            raise ToolInputError(
                f"filter_geometry must be a GeoJSON object "
                f"(got {type(geojson).__name__})"
            )
        gj_type = geojson.get("type", "")
        if gj_type == "Feature":
            return cls._geojson_to_esri_polygon(geojson.get("geometry") or {})
        if gj_type == "Polygon":
            rings = list(geojson.get("coordinates") or [])
        elif gj_type == "MultiPolygon":
            rings = []
            for poly in geojson.get("coordinates") or []:
                rings.extend(poly)
        else:
            raise ToolInputError(
                f"filter_geometry must be a GeoJSON Polygon, MultiPolygon, or "
                f"Feature wrapping one (got type={gj_type!r})"
            )
        if not rings:
            raise ToolInputError("filter_geometry has no polygon rings")
        if len(rings) > cls.MAX_FILTER_RINGS:
            raise ToolInputError(
                f"filter_geometry has {len(rings)} rings; max is "
                f"{cls.MAX_FILTER_RINGS}. Simplify the polygon or use "
                f"filter_item_id with a published boundary layer."
            )
        coord_count = sum(len(r) for r in rings if isinstance(r, list))
        if coord_count > cls.MAX_FILTER_COORDS:
            raise ToolInputError(
                f"filter_geometry has {coord_count} coordinates; max is "
                f"{cls.MAX_FILTER_COORDS}. Simplify the polygon or use "
                f"filter_item_id with a published boundary layer."
            )
        return {"rings": rings, "spatialReference": {"wkid": 4326}}

    @classmethod
    def _union_esri_rings(
        cls, rings: List[List[List[float]]]
    ) -> Optional[List[List[List[float]]]]:
        """Geometrically union possibly overlapping rings.

        Concatenating the rings of several features is NOT a union: under the
        even-odd fill rule, overlapping same-orientation rings flip covered
        area into holes, so the filter region SHRINKS as features are added.
        pyclipper resolves them into clean non-overlapping rings. Returns
        rings in Esri orientation, or None when the union is empty or
        pyclipper rejects the input -- callers fall back to the raw rings.
        """
        paths = cls._rings_to_paths(rings)
        if not paths:
            return None
        try:
            pc = pyclipper.Pyclipper()
            pc.AddPaths(paths, pyclipper.PT_SUBJECT, True)
            solution = pc.Execute(
                pyclipper.CT_UNION, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO
            )
        except pyclipper.ClipperException:
            return None
        # Clipper emits CCW exteriors / CW holes (y-up); Esri wants the
        # opposite, so reverse every ring.
        return cls._paths_to_rings(solution, reverse=True) or None

    @classmethod
    def _rings_to_paths(cls, rings: List[Any]) -> List[List[Tuple[int, int]]]:
        """Closed coordinate rings -> open integer pyclipper paths."""
        scale = cls._UNION_DEG_SCALE
        paths = []
        for ring in rings or []:
            if not isinstance(ring, list):
                continue
            path = [
                (int(round(pt[0] * scale)), int(round(pt[1] * scale)))
                for pt in ring
                if isinstance(pt, (list, tuple)) and len(pt) >= 2
            ]
            if len(path) > 1 and path[0] == path[-1]:
                path.pop()
            if len(path) >= 3:
                paths.append(path)
        return paths

    @classmethod
    def _paths_to_rings(
        cls, paths: List[Any], reverse: bool = False
    ) -> List[List[List[float]]]:
        """Integer pyclipper paths -> closed coordinate rings."""
        scale = cls._UNION_DEG_SCALE
        out: List[List[List[float]]] = []
        for path in paths:
            if len(path) < 3:
                continue
            pts = reversed(path) if reverse else path
            ring = [[x / scale, y / scale] for x, y in pts]
            ring.append(list(ring[0]))
            out.append(ring)
        return out

    async def _fetch_filter_polygon(
        self, filter_item_id: str, filter_where: str
    ) -> Tuple[Dict[str, Any], int]:
        """Resolve a filter polygon from feature(s) of another layer.

        Validates that the layer is polygonal, refuses LOUDLY when the WHERE
        matches more than MAX_FILTER_FEATURES (silently unioning a truncated
        subset is exactly the quiet false negative to avoid), and returns the
        unioned Esri polygon plus the feature count.
        """
        validated_where = WhereValidator.validate(filter_where or "1=1")
        layer_url = await self._layer_url_for_item(filter_item_id)
        try:
            meta_resp = await self.feature_client.get(layer_url, params={"f": "json"})
            meta_resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service metadata error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e
        meta = meta_resp.json()
        if "error" in meta:
            raise RuntimeError(
                f"Feature Service metadata error: "
                f"{meta['error'].get('message', str(meta['error']))}"
            )
        geom_type = meta.get("geometryType", "")
        if geom_type != "esriGeometryPolygon":
            raise ToolInputError(
                f"filter_item_id must be a polygon layer (got "
                f"geometryType={geom_type or 'unknown'!r}); check it with "
                f"get_layer_schema."
            )

        count_data = await self._query_layer(
            layer_url,
            {"where": validated_where, "returnCountOnly": "true", "f": "json"},
        )
        match_count = int(count_data.get("count") or 0)
        if match_count > self.MAX_FILTER_FEATURES:
            raise ToolInputError(
                f"filter_where {validated_where!r} matches {match_count:,} "
                f"features in filter layer {filter_item_id}; max is "
                f"{self.MAX_FILTER_FEATURES:,} for a spatial filter. Narrow "
                f"the WHERE clause."
            )
        if match_count == 0:
            raise ToolInputError(
                f"filter_where {validated_where!r} matched no features in "
                f"filter layer {filter_item_id}; confirm the value with "
                f"get_distinct_values."
            )

        features: List[Dict[str, Any]] = []
        while len(features) < match_count:
            data = await self._query_layer(
                layer_url,
                {
                    "where": validated_where,
                    "outFields": "",
                    "returnGeometry": "true",
                    "outSR": 4326,
                    "resultRecordCount": self.FILTER_FETCH_PAGE,
                    "resultOffset": len(features),
                    "f": "json",
                },
                post=True,
            )
            page = data.get("features", [])
            if not page:
                break
            features.extend(page)

        rings: List[Any] = []
        for f in features:
            rings.extend((f.get("geometry") or {}).get("rings") or [])
        if not rings:
            raise ToolInputError(
                f"The {len(features)} filter feature(s) in {filter_item_id} "
                f"have no polygon rings"
            )
        # One feature's rings are already a coherent polygon: pass them
        # through untouched. Several need a REAL union (see _union_esri_rings).
        if len(features) > 1:
            rings = self._union_esri_rings(rings) or rings
        return {"rings": rings, "spatialReference": {"wkid": 4326}}, len(features)

    async def geocode_address(self, address: str) -> List[Dict[str, Any]]:
        """Geocode a street address to WGS84 lon/lat.

        Uses the configured ArcGIS GeocodeServer (`geocoder_url`, e.g. the
        SANDAG composite locator) when set; otherwise falls back to the free,
        key-less US Census geocoder. Returns candidates with matched_address,
        lon, and lat.
        """
        if not address or not address.strip():
            raise ToolInputError("address is required")

        if self.plugin_config and self.plugin_config.geocoder_url:
            return await self._geocode_arcgis(address)

        # Census fallback: if `geocoder_region` is configured (e.g.
        # 'San Diego, CA') it is appended to bias results to this region.
        region = (
            self.plugin_config.geocoder_region if self.plugin_config else ""
        ) or ""
        full = address
        if region and region.lower() not in address.lower():
            full = f"{address}, {region}"

        params = {
            "address": full,
            "benchmark": "Public_AR_Current",
            "format": "json",
        }
        try:
            response = await self.feature_client.get(
                _CENSUS_GEOCODER_URL, params=params
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Geocoder error (HTTP {e.response.status_code}): {e.response.text}"
            ) from e

        matches = response.json().get("result", {}).get("addressMatches", [])
        results = []
        for m in matches:
            coords = m.get("coordinates", {})
            if coords.get("x") is not None and coords.get("y") is not None:
                results.append(
                    {
                        "matched_address": m.get("matchedAddress", ""),
                        "lon": coords["x"],
                        "lat": coords["y"],
                    }
                )
        return results

    async def _geocode_arcgis(self, address: str) -> List[Dict[str, Any]]:
        """Geocode via the configured ArcGIS GeocodeServer
        (findAddressCandidates), pinned to WGS84 output."""
        params = {
            "SingleLine": address,
            "outSR": 4326,
            "maxLocations": 5,
            "f": "json",
        }
        try:
            response = await self.feature_client.get(
                f"{self.plugin_config.geocoder_url}/findAddressCandidates",
                params=params,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Geocoder error (HTTP {e.response.status_code}): {e.response.text}"
            ) from e

        data = response.json()
        if "error" in data:
            raise RuntimeError(
                f"Geocoder error: {data['error'].get('message', str(data['error']))}"
            )
        results = []
        for c in data.get("candidates", []):
            loc = c.get("location") or {}
            if loc.get("x") is None or loc.get("y") is None:
                continue
            if c.get("score", 0) < 60:
                continue  # drop weak matches the locator itself doubts
            results.append(
                {
                    "matched_address": c.get("address", ""),
                    "lon": loc["x"],
                    "lat": loc["y"],
                }
            )
        return results

    # ── Health check ────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            if self._search_mode == "portal" and self.portal_client:
                response = await self.portal_client.get(
                    "/sharing/rest/search",
                    params={"q": 'type:"Feature Service"', "num": 1, "f": "json"},
                )
            else:
                response = await self.feature_client.get(
                    self.plugin_config.services_url, params={"f": "json"}
                )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    # ── Private helpers ─────────────────────────────────────────────────

    async def _ensure_layer_url(self, service_url: str) -> str:
        """Resolve a Feature/Map Server URL to a specific queryable layer URL.

        If the URL already targets a layer (e.g. ``.../FeatureServer/3``) it is
        returned unchanged. If it points at the service root
        (e.g. ``.../FeatureServer``) the service metadata is fetched and the
        first published layer's id is used. Layers are not guaranteed to start
        at index 0 -- services derived from the MassGIS parcel standard, for
        instance, publish their only layer at index 1 -- so assuming ``/0``
        silently breaks queries against them. Falls back to ``/0`` if the
        service metadata cannot be read.
        """
        stripped = service_url.rstrip("/")
        if not re.search(r"/(FeatureServer|MapServer)$", stripped, re.IGNORECASE):
            # Already targets a specific layer, or isn't a recognized service root.
            return stripped

        layer_id: Any = 0
        try:
            response = await self.feature_client.get(stripped, params={"f": "json"})
            response.raise_for_status()
            meta = response.json()
            candidates = meta.get("layers") or meta.get("tables") or []
            first_id = candidates[0].get("id") if candidates else None
            if first_id is not None:
                layer_id = first_id
        except Exception as e:
            logger.warning(
                f"Could not read service metadata for {stripped}; "
                f"defaulting to layer 0: {e}"
            )
        return f"{stripped}/{layer_id}"

    @staticmethod
    def _epoch_ms_to_iso(epoch_ms: Any) -> str:
        if epoch_ms is None:
            return ""
        try:
            return datetime.fromtimestamp(int(epoch_ms) / 1000).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            return ""

    @staticmethod
    def _clean_text(value: Any) -> str:
        """Strip HTML and normalize to readable ASCII.

        Hub descriptions are HTML with smart quotes, em-dashes, and
        non-breaking spaces. Unescape entities, drop tags, map common unicode
        punctuation to ASCII, then transliterate/drop anything still non-ASCII
        and collapse whitespace.
        """
        if value is None:
            return ""
        text = html.unescape(str(value))
        text = _HTML_TAG_RE.sub(" ", text)
        for uni, ascii_ in _UNICODE_PUNCT.items():
            text = text.replace(uni, ascii_)
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _extract_dataset_summary(props: Dict[str, Any]) -> Dict[str, Any]:
        description = ArcGISPlugin._clean_text(props.get("description", "") or "")
        if len(description) > 300:
            description = description[:300] + "..."

        return {
            "id": props.get("id", ""),
            "title": props.get("title", ""),
            "description": description,
            "type": props.get("type", ""),
            "url": props.get("url", ""),
            "access": props.get("access", ""),
            "owner": props.get("owner", ""),
            "created": ArcGISPlugin._epoch_ms_to_iso(props.get("created")),
            "modified": ArcGISPlugin._epoch_ms_to_iso(props.get("modified")),
            "tags": props.get("tags", []),
            "extent": props.get("extent", []),
        }

    def _format_search_results(self, datasets: List[Dict[str, Any]]) -> str:
        if not datasets:
            return "Found 0 dataset(s)."

        lines = [f"Found {len(datasets)} dataset(s):\n"]

        for i, ds in enumerate(datasets, 1):
            tags = ", ".join(ds.get("tags", [])) if ds.get("tags") else "None"
            lines.append(f"{i}. {ds.get('title', 'Untitled')}")
            lines.append(f"   ID: {ds.get('id', 'unknown')}")
            lines.append(f"   Type: {ds.get('type', 'unknown')}")
            lines.append(f"   Access: {ds.get('access', 'unknown')}")
            lines.append(f"   Description: {ds.get('description', 'No description')}")
            lines.append(f"   URL: {ds.get('url', '')}")
            lines.append(f"   Tags: {tags}")
            lines.append("")

        return "\n".join(lines)

    def _format_dataset(self, dataset: Dict[str, Any]) -> str:
        tags = ", ".join(dataset.get("tags", [])) if dataset.get("tags") else "None"
        lines = [
            f"Dataset: {dataset.get('title', 'Untitled')}",
            f"ID: {dataset.get('id', 'unknown')}",
            f"Type: {dataset.get('type', 'unknown')}",
            f"Access: {dataset.get('access', 'unknown')}",
            f"Owner: {dataset.get('owner', 'unknown')}",
            f"Created: {dataset.get('created', '')}",
            f"Modified: {dataset.get('modified', '')}",
            f"Description: {dataset.get('description', 'No description')}",
            f"Snippet: {dataset.get('snippet', '')}",
            f"License: {dataset.get('licenseInfo', '')}",
            f"Spatial Reference: {dataset.get('spatialReference', '')}",
            f"Geometry Type: {dataset.get('geometryType', '')}",
            f"Number of Records: {dataset.get('numRecords', 'N/A')}",
            f"Attribution: {dataset.get('attribution', '')}",
            f"Tags: {tags}",
            f"Extent: {dataset.get('extent', [])}",
            f"Additional Resources: {dataset.get('additionalResources', [])}",
            f"URL: {dataset.get('url', '')}",
            f"Service URL (use for query_data): {dataset.get('service_url', '')}",
        ]
        return "\n".join(lines)

    def _format_query_results(
        self,
        records: List[Dict[str, Any]],
        limit: int,
        total: Optional[int] = None,
        attribution: str = "",
    ) -> str:
        # SanGIS requires its attribution to travel with the data.
        footer = f"\n\nData attribution: {attribution}" if attribution else ""

        if not records:
            if total is not None:
                return (
                    f"TOTAL MATCHING: {total}\nReturned 0 record(s) (limit: {limit})."
                    + footer
                )
            return f"Returned 0 record(s) (limit: {limit})." + footer

        lines = []
        if total is not None:
            lines.append(f"TOTAL MATCHING: {total}")
        lines.append(f"Returned {len(records)} record(s) (limit: {limit}):")
        lines.append("")

        for i, record in enumerate(records, 1):
            lines.append(f"Record {i}:")
            for key, value in record.items():
                clean = self._clean_text(value) if isinstance(value, str) else value
                lines.append(f"  {key}: {clean}")
            lines.append("")

        return "\n".join(lines) + footer

    def _format_aggregations(self, field: str, buckets: List[Dict[str, Any]]) -> str:
        if not buckets:
            return f"Aggregations for '{field}': 0 bucket(s)."

        lines = [f"Aggregations for '{field}':\n"]
        for bucket in buckets:
            lines.append(
                f"  {bucket.get('key', 'unknown')}: "
                f"{bucket.get('doc_count', bucket.get('count', 0))} dataset(s)"
            )

        return "\n".join(lines)

    def _format_layer_schema(self, schema: Dict[str, Any]) -> str:
        fields = schema.get("fields", [])
        if not fields:
            return f"Layer: {schema.get('layer_name', '')}\nFields (0)."

        lines = [
            f"Layer: {schema.get('layer_name', '')}",
            f"Geometry: {schema.get('geometry_type', '') or 'none (table)'}",
        ]
        if schema.get("copyright"):
            lines.append(f"Attribution: {schema['copyright']}")
        lines += [
            f"Fields ({len(fields)}):",
            "",
        ]
        for f in fields:
            name = f.get("name", "")
            ftype = (f.get("type", "") or "").replace("esriFieldType", "")
            alias = f.get("alias", "")
            line = f"  {name} ({ftype})"
            if alias and alias != name:
                line += f" -- {alias}"
            lines.append(line)
            domain = f.get("domain") or {}
            coded = domain.get("codedValues") if isinstance(domain, dict) else None
            if coded:
                sample = ", ".join(
                    f"{c.get('code')}={c.get('name')}" for c in coded[:8]
                )
                more = " ..." if len(coded) > 8 else ""
                lines.append(f"      coded values: {sample}{more}")
        return "\n".join(lines)

    def _format_distinct_values(self, field: str, values: List[Any]) -> str:
        if not values:
            return f"0 distinct value(s) for '{field}'."

        lines = [f"{len(values)} distinct value(s) for '{field}':", ""]
        for v in values:
            lines.append(f"  {v}")
        return "\n".join(lines)

    def _format_geocode(self, address: str, candidates: List[Dict[str, Any]]) -> str:
        if not candidates:
            return f"0 match(es) for '{address}'."
        lines = [f"{len(candidates)} match(es) for '{address}':", ""]
        for c in candidates:
            lines.append(f"  {c.get('matched_address', '')}")
            lines.append(f"    lon: {c.get('lon')}, lat: {c.get('lat')}")
        return "\n".join(lines)
