"""MCP conformance for the arcgis plugin: structured output, error
classification, and their drift guards.

A declared outputSchema is BINDING: the spec says servers MUST return
conforming results. So every successful code path of every tool -- the
empty, truncated, clamped, count-failed, snapped and geocoded branches,
not just the happy path -- is exercised here and validated against the
schema the server itself advertises. The caveat/prose parity rule is
asserted on every one of them. Caller mistakes must log at WARNING with
no traceback; genuine upstream faults must keep theirs.
"""

import ast
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from jsonschema import Draft202012Validator

from core.interfaces import ToolInputError
from core.mcp_server import MCPServer
from core.plugin_manager import PluginManager
from plugins.arcgis.config_schema import ArcGISPluginConfig
from plugins.arcgis.plugin import CAVEAT_CODES, ArcGISPlugin
from plugins.arcgis.where_validator import WhereValidator

ITEM_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SERVICE_URL = (
    "https://geo.example.gov/server/rest/services/Hosted/Parcels/FeatureServer/0"
)

DATASET = {
    "id": ITEM_ID,
    "title": "Parcels",
    "type": "Feature Service",
    "url": SERVICE_URL.rsplit("/", 1)[0],
    "access": "public",
    "owner": "sangis",
    "created": "2024-01-01",
    "modified": "2026-08-01",
    "description": "Assessor parcels",
    "tags": ["parcels", "cadastral"],
    "extent": [],
    "service_url": SERVICE_URL,
    "attribution": "SanGIS",
}

PORTAL_ITEM = {
    "id": ITEM_ID,
    "title": "Parcels",
    "type": "Feature Service",
    "url": SERVICE_URL.rsplit("/", 1)[0],
    "access": "public",
    "owner": "sangis",
    "created": 1704067200000,
    "modified": 1722470400000,
    "description": "Assessor parcels",
    "tags": ["parcels"],
    "extent": [[-117.6, 32.5], [-116.1, 33.5]],
    "accessInformation": "SanGIS",
}

LAYER_META = {
    "name": "Parcels",
    "geometryType": "esriGeometryPolygon",
    "copyrightText": "SanGIS",
    "fields": [
        {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"},
        {"name": "APN", "type": "esriFieldTypeString", "alias": "APN"},
        {
            "name": "situs_juris",
            "type": "esriFieldTypeString",
            "alias": "Jurisdiction",
            "domain": {"codedValues": [{"code": "SD", "name": "San Diego"}]},
        },
    ],
}


def resp(payload, status_code=200):
    r = Mock()
    r.status_code = status_code
    r.raise_for_status = Mock()
    r.headers = {"content-type": "application/json"}
    r.json = Mock(return_value=payload)
    return r


def page(records, exceeded=False):
    payload = {"features": [{"attributes": r} for r in records]}
    if exceeded:
        payload["exceededTransferLimit"] = True
    return resp(payload)


def count(n):
    return resp({"count": n})


def census(*matches):
    return resp(
        {
            "result": {
                "addressMatches": [
                    {"matchedAddress": addr, "coordinates": {"x": lon, "y": lat}}
                    for addr, lon, lat in matches
                ]
            }
        }
    )


@pytest.fixture
def plugin():
    cfg = {
        "portal_url": "https://geo.example.gov/portal",
        "services_url": "https://geo.example.gov/server/rest/services",
        "city_name": "TestRegion",
        "timeout": 20,
        "geocoder_region": "San Diego, CA",
    }
    p = ArcGISPlugin(cfg)
    p.plugin_config = ArcGISPluginConfig(**cfg)
    p.feature_client = AsyncMock()
    p.portal_client = AsyncMock()
    p._search_mode = "portal"
    return p


def schema_for(plugin, tool_name):
    return next(t.output_schema for t in plugin.get_tools() if t.name == tool_name)


def assert_conforms(plugin, tool_name, result):
    """Validate structuredContent against the advertised schema and assert
    that every structured caveat appears verbatim in the prose."""
    assert result.success, result.error_message
    structured = result.structured_content
    assert structured is not None, f"{tool_name} returned no structured_content"
    Draft202012Validator(schema_for(plugin, tool_name)).validate(structured)
    text = result.content[0]["text"]
    for caveat in structured["caveats"]:
        assert caveat["message"] in text, (
            f"{tool_name}: caveat {caveat['code']} is in structuredContent "
            f"but not in the rendered text"
        )
    return structured


def codes(structured):
    return [c["code"] for c in structured["caveats"]]


def with_dataset(plugin, dataset=DATASET):
    return patch.object(
        plugin, "get_dataset", new_callable=AsyncMock, return_value=dataset
    )


# ── schema declarations ────────────────────────────────────────────────


class TestOutputSchemaDeclarations:
    PAYLOAD_KEYS = {
        "search_datasets": "datasets",
        "get_dataset": "dataset",
        "get_aggregations": "buckets",
        "query_data": "rows",
        "get_layer_schema": "fields",
        "get_distinct_values": "values",
        "spatial_query_point": "rows",
        "spatial_query_polygon": "rows",
        "geocode_address": "candidates",
    }

    def test_every_tool_declares_a_valid_output_schema(self, plugin):
        for t in plugin.get_tools():
            assert t.output_schema, f"{t.name} has no output_schema"
            Draft202012Validator.check_schema(t.output_schema)

    def test_output_schema_is_advertised_on_the_wire(self, plugin):
        manager = PluginManager({})
        manager.plugins = {"arcgis": plugin}
        for tool in manager.get_all_tools():
            assert "outputSchema" in tool, tool["name"]
            assert "title" in tool and tool["annotations"]["readOnlyHint"] is True

    def test_every_schema_uses_the_shared_envelope(self, plugin):
        """One shape across the server: the envelope keys plus exactly one
        payload key named for its contents."""
        for t in plugin.get_tools():
            required = set(t.output_schema["required"])
            assert required == {
                "query",
                "summary",
                "caveats",
                self.PAYLOAD_KEYS[t.name],
            }

    def test_caveat_enum_matches_the_code_constant(self, plugin):
        for t in plugin.get_tools():
            enum = t.output_schema["properties"]["caveats"]["items"]["properties"][
                "code"
            ]["enum"]
            assert enum == list(CAVEAT_CODES), t.name

    @pytest.mark.asyncio
    async def test_caller_errors_carry_no_structured_content(self, plugin):
        result = await plugin.execute_tool("get_dataset", {})
        assert result.success is False
        assert result.structured_content is None
        assert "dataset_id is required" in result.error_message


# ── error classification drift guards ──────────────────────────────────


class TestErrorClassificationDoesNotDrift:
    PLUGIN_SRC = Path(__file__).resolve().parents[1] / "plugins/arcgis/plugin.py"
    VALIDATOR_SRC = (
        Path(__file__).resolve().parents[1] / "plugins/arcgis/where_validator.py"
    )

    def test_only_the_upstream_fault_raises_a_plain_value_error(self):
        """Exactly one plain ValueError: the Feature Service returning
        non-JSON. Everything the caller can cause is a ToolInputError."""
        src = self.PLUGIN_SRC.read_text(encoding="utf-8")
        assert src.count("raise ValueError(") == 1, (
            "a new plain ValueError was added to the arcgis plugin -- classify "
            "it deliberately: caller mistake -> ToolInputError, genuine "
            "upstream/server fault -> ValueError (keeps its traceback)"
        )
        assert "returned non-JSON response" in src

    def test_shared_validators_only_raise_caller_errors(self):
        src = self.VALIDATOR_SRC.read_text(encoding="utf-8")
        assert src.count("raise ValueError(") == 0

    def test_every_numeric_coercion_of_caller_input_is_guarded(self):
        tree = ast.parse(self.PLUGIN_SRC.read_text(encoding="utf-8"))
        guarded = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            handled = any(
                isinstance(h, ast.ExceptHandler)
                and (
                    "ToolInputError" in ast.unparse(h)
                    or "ValueError" in ast.unparse(h.type or ast.Constant(""))
                )
                for h in node.handlers
            )
            if handled:
                for stmt in node.body:
                    for sub in ast.walk(stmt):
                        if hasattr(sub, "lineno"):
                            guarded.add(sub.lineno)
        unguarded = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("int", "float")
            ):
                inner = ast.unparse(node)
                if (
                    any(tok in inner for tok in ("arguments.get", "arguments["))
                    and node.lineno not in guarded
                ):
                    unguarded.append(f"line {node.lineno}: {inner}")
        assert not unguarded, "\n".join(unguarded)


class TestCallerErrorLogging:
    @staticmethod
    def _records(caplog, level=logging.WARNING):
        return [r for r in caplog.records if r.levelno >= level]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool,args,fragment",
        [
            ("get_dataset", {"dataset_id": "nope"}, "Invalid dataset ID"),
            ("query_data", {"dataset_id": ITEM_ID, "limit": "many"}, "limit must be"),
            ("search_datasets", {"q": "x", "limit": "lots"}, "limit must be"),
            (
                "spatial_query_point",
                {"item_id": ITEM_ID, "lon": -200, "lat": 32.5},
                "lon must be between",
            ),
            (
                "spatial_query_point",
                {"item_id": ITEM_ID, "lon": "west", "lat": 32.5},
                "lon must be a number",
            ),
            ("get_aggregations", {"field": "colour"}, "not an aggregatable field"),
        ],
    )
    async def test_bad_arguments_log_warning_never_a_traceback(
        self, plugin, caplog, tool, args, fragment
    ):
        with caplog.at_level(logging.WARNING):
            result = await plugin.execute_tool(tool, args)
        assert result.success is False
        assert fragment in result.error_message
        records = self._records(caplog)
        assert records, "expected a WARNING record"
        assert all(r.levelno == logging.WARNING for r in records)
        assert all(r.exc_info is None for r in records)

    @pytest.mark.asyncio
    async def test_rejected_where_clause_is_a_caller_error(self, plugin, caplog):
        with with_dataset(plugin), caplog.at_level(logging.WARNING):
            result = await plugin.execute_tool(
                "query_data", {"dataset_id": ITEM_ID, "where": "1=1; DROP TABLE x"}
            )
        assert result.success is False
        assert "Forbidden" in result.error_message
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    @pytest.mark.asyncio
    async def test_upstream_fault_still_logs_error_with_traceback(self, plugin, caplog):
        bad = resp({})
        bad.headers = {"content-type": "text/html"}
        bad.json = Mock(side_effect=ValueError("no json"))
        plugin.feature_client.get = AsyncMock(return_value=bad)
        with with_dataset(plugin), caplog.at_level(logging.WARNING):
            result = await plugin.execute_tool("query_data", {"dataset_id": ITEM_ID})
        assert result.success is False
        assert "non-JSON" in result.error_message
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors and any(r.exc_info is not None for r in errors)

    def test_tool_input_error_is_a_value_error(self):
        assert issubclass(ToolInputError, ValueError)

    def test_where_validator_masks_quoted_literals(self):
        assert (
            WhereValidator.validate("OWNER = 'SMITH; JONES'")
            == "OWNER = 'SMITH; JONES'"
        )
        with pytest.raises(ToolInputError, match="Unbalanced"):
            WhereValidator.validate("NAME = 'open")


# ── conformance per tool, per branch ───────────────────────────────────


class TestSearchDatasets:
    @pytest.mark.asyncio
    async def test_hit(self, plugin):
        plugin.portal_client.get = AsyncMock(
            return_value=resp({"results": [PORTAL_ITEM]})
        )
        result = await plugin.execute_tool(
            "search_datasets", {"q": "parcels", "type": "Feature Service"}
        )
        s = assert_conforms(plugin, "search_datasets", result)
        assert s["summary"]["returned"] == 1
        assert s["summary"]["search_mode"] == "portal"
        assert s["datasets"][0]["id"] == ITEM_ID
        assert s["datasets"][0]["created"]  # epoch ms rendered as an ISO date
        assert s["query"]["type"] == "Feature Service"
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_empty(self, plugin):
        plugin.portal_client.get = AsyncMock(return_value=resp({"results": []}))
        result = await plugin.execute_tool("search_datasets", {"q": "qwzxjv"})
        s = assert_conforms(plugin, "search_datasets", result)
        assert s["datasets"] == []
        assert codes(s) == ["no_results"]

    @pytest.mark.asyncio
    async def test_limit_clamped(self, plugin):
        plugin.portal_client.get = AsyncMock(
            return_value=resp({"results": [PORTAL_ITEM]})
        )
        result = await plugin.execute_tool("search_datasets", {"q": "p", "limit": 500})
        s = assert_conforms(plugin, "search_datasets", result)
        assert s["query"]["limit"] == 100
        assert "limit_clamped" in codes(s)


class TestGetDataset:
    @pytest.mark.asyncio
    async def test_portal_item(self, plugin):
        plugin.portal_client.get = AsyncMock(return_value=resp(PORTAL_ITEM))
        result = await plugin.execute_tool("get_dataset", {"dataset_id": ITEM_ID})
        s = assert_conforms(plugin, "get_dataset", result)
        assert s["summary"]["queryable"] is True
        assert s["summary"]["attribution"] == "SanGIS"
        assert s["dataset"]["service_url"].endswith("/FeatureServer")
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_non_queryable_item(self, plugin):
        item = {**PORTAL_ITEM, "type": "Web Map", "url": ""}
        plugin.portal_client.get = AsyncMock(return_value=resp(item))
        result = await plugin.execute_tool("get_dataset", {"dataset_id": ITEM_ID})
        s = assert_conforms(plugin, "get_dataset", result)
        assert s["summary"]["queryable"] is False

    @pytest.mark.asyncio
    async def test_directory_service(self, plugin):
        plugin.feature_client.get = AsyncMock(
            return_value=resp(
                {"serviceDescription": "Parcels", "copyrightText": "SanGIS"}
            )
        )
        result = await plugin.execute_tool(
            "get_dataset", {"dataset_id": "Hosted/Parcels/FeatureServer"}
        )
        s = assert_conforms(plugin, "get_dataset", result)
        assert s["dataset"]["id"] == "Hosted/Parcels/FeatureServer"
        assert s["summary"]["queryable"] is True


class TestGetAggregations:
    @pytest.mark.asyncio
    async def test_type_facets(self, plugin):
        items = [PORTAL_ITEM, {**PORTAL_ITEM, "id": "b" * 32, "type": "Web Map"}]
        plugin.portal_client.get = AsyncMock(return_value=resp({"results": items}))
        result = await plugin.execute_tool("get_aggregations", {"field": "type"})
        s = assert_conforms(plugin, "get_aggregations", result)
        assert s["summary"]["bucket_count"] == 2
        assert s["summary"]["items_counted"] == 2
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_empty(self, plugin):
        plugin.portal_client.get = AsyncMock(return_value=resp({"results": []}))
        result = await plugin.execute_tool(
            "get_aggregations", {"field": "owner", "q": "zz"}
        )
        s = assert_conforms(plugin, "get_aggregations", result)
        assert s["buckets"] == []
        assert codes(s) == ["no_results"]


class TestQueryData:
    @pytest.mark.asyncio
    async def test_happy_path_with_count_and_attribution(self, plugin):
        plugin.feature_client.get = AsyncMock(
            side_effect=[page([{"APN": "1"}]), count(1)]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool("query_data", {"dataset_id": ITEM_ID})
        s = assert_conforms(plugin, "query_data", result)
        assert s["rows"] == [{"APN": "1"}]
        assert s["summary"]["total_matching"] == 1
        assert s["summary"]["truncated"] is False
        assert s["summary"]["pages_fetched"] == 1
        assert s["summary"]["attribution"] == "SanGIS"
        assert "Data attribution: SanGIS" in result.content[0]["text"]
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_truncated_by_limit(self, plugin):
        plugin.feature_client.get = AsyncMock(
            side_effect=[page([{"A": 1}, {"A": 2}]), count(999)]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "query_data", {"dataset_id": ITEM_ID, "limit": 2}
            )
        s = assert_conforms(plugin, "query_data", result)
        assert s["summary"]["truncated"] is True
        assert "results_truncated" in codes(s)
        assert "of 999" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_count_failure_is_null_not_zero(self, plugin):
        plugin.feature_client.get = AsyncMock(
            side_effect=[page([{"A": 1}]), RuntimeError("count exploded")]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool("query_data", {"dataset_id": ITEM_ID})
        s = assert_conforms(plugin, "query_data", result)
        assert s["summary"]["total_matching"] is None
        assert s["summary"]["truncated"] is False
        assert codes(s) == ["count_unavailable"]
        assert "TOTAL MATCHING" not in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_exceeded_transfer_limit_without_count(self, plugin):
        plugin.feature_client.get = AsyncMock(
            side_effect=[page([{"A": 1}], exceeded=True), RuntimeError("x")]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "query_data", {"dataset_id": ITEM_ID, "limit": 1}
            )
        s = assert_conforms(plugin, "query_data", result)
        assert s["summary"]["truncated"] is True
        assert set(codes(s)) == {"count_unavailable", "results_truncated"}

    @pytest.mark.asyncio
    async def test_paged(self, plugin):
        """A server page smaller than the limit sets exceededTransferLimit;
        the second page completes the result."""
        plugin.feature_client.get = AsyncMock(
            side_effect=[page([{"A": 1}], exceeded=True), page([{"A": 2}]), count(2)]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "query_data", {"dataset_id": ITEM_ID, "limit": 10}
            )
        s = assert_conforms(plugin, "query_data", result)
        assert s["summary"]["returned"] == 2
        assert s["summary"]["pages_fetched"] == 2
        assert s["summary"]["truncated"] is False

    @pytest.mark.asyncio
    async def test_empty(self, plugin):
        plugin.feature_client.get = AsyncMock(side_effect=[page([]), count(0)])
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "query_data", {"dataset_id": ITEM_ID, "where": "APN = 'nope'"}
            )
        s = assert_conforms(plugin, "query_data", result)
        assert s["rows"] == []
        assert codes(s) == ["no_results"]
        assert "TOTAL MATCHING: 0" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_limit_clamped(self, plugin):
        plugin.feature_client.get = AsyncMock(side_effect=[page([{"A": 1}]), count(1)])
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "query_data", {"dataset_id": ITEM_ID, "limit": 5000}
            )
        s = assert_conforms(plugin, "query_data", result)
        assert s["query"]["limit"] == 1000
        assert "limit_clamped" in codes(s)

    @pytest.mark.asyncio
    async def test_non_queryable_dataset_is_a_caller_error(self, plugin, caplog):
        with (
            with_dataset(plugin, {**DATASET, "type": "Web Map"}),
            caplog.at_level(logging.WARNING),
        ):
            result = await plugin.execute_tool("query_data", {"dataset_id": ITEM_ID})
        assert result.success is False
        assert "not queryable" in result.error_message
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


class TestGetLayerSchema:
    @pytest.mark.asyncio
    async def test_fields(self, plugin):
        plugin.feature_client.get = AsyncMock(return_value=resp(LAYER_META))
        with with_dataset(plugin):
            result = await plugin.execute_tool("get_layer_schema", {"item_id": ITEM_ID})
        s = assert_conforms(plugin, "get_layer_schema", result)
        assert s["summary"]["field_count"] == 3
        assert s["summary"]["attribution"] == "SanGIS"
        assert s["fields"][2]["domain"]["codedValues"][0]["code"] == "SD"
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_keyword_no_match(self, plugin):
        plugin.feature_client.get = AsyncMock(return_value=resp(LAYER_META))
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "get_layer_schema", {"item_id": ITEM_ID, "keyword": "acreage"}
            )
        s = assert_conforms(plugin, "get_layer_schema", result)
        assert s["fields"] == []
        assert s["summary"]["filtered"] is True
        assert codes(s) == ["no_results"]


class TestGetDistinctValues:
    @pytest.mark.asyncio
    async def test_values(self, plugin):
        plugin.feature_client.get = AsyncMock(
            return_value=page([{"situs_juris": "SD"}, {"situs_juris": None}])
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "get_distinct_values", {"item_id": ITEM_ID, "field": "situs_juris"}
            )
        s = assert_conforms(plugin, "get_distinct_values", result)
        assert s["values"] == ["SD", None]
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_truncated_and_empty(self, plugin):
        plugin.feature_client.get = AsyncMock(
            return_value=page([{"Z": "A"}, {"Z": "B"}])
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "get_distinct_values", {"item_id": ITEM_ID, "field": "Z", "limit": 2}
            )
        s = assert_conforms(plugin, "get_distinct_values", result)
        assert codes(s) == ["results_truncated"]

        plugin.feature_client.get = AsyncMock(return_value=page([]))
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "get_distinct_values", {"item_id": ITEM_ID, "field": "Z", "like": "q"}
            )
        s = assert_conforms(plugin, "get_distinct_values", result)
        assert s["values"] == []
        assert codes(s) == ["no_results"]


FILTER_ID = "ffffffffffffffffffffffffffffffff"
SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [[-117.2, 32.7], [-117.1, 32.7], [-117.1, 32.8], [-117.2, 32.8], [-117.2, 32.7]]
    ],
}


def esri_square(x0, y0, x1, y1):
    """One closed ring in Esri (clockwise, y-up) orientation."""
    return {"rings": [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]}


class TestSpatialQueryPolygon:
    @pytest.mark.asyncio
    async def test_inline_geometry_with_buffer(self, plugin):
        plugin.feature_client.post = AsyncMock(
            side_effect=[page([{"name": "Lib A"}]), count(3)]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_polygon",
                {
                    "item_id": ITEM_ID,
                    "filter_geometry": SQUARE,
                    "distance": 1,
                    "units": "miles",
                    "limit": 1,
                },
            )
        s = assert_conforms(plugin, "spatial_query_polygon", result)
        assert s["rows"] == [{"name": "Lib A"}]
        assert s["summary"]["total_matching"] == 3
        assert s["summary"]["filter_features"] is None
        assert s["summary"]["truncated"] is True
        assert s["summary"]["attribution"] == "SanGIS"
        assert s["query"]["filter_geometry_type"] == "Polygon"
        assert s["query"]["filter_where"] is None
        assert s["query"]["units"] == "miles"
        assert codes(s) == ["results_truncated"]
        assert "TOTAL MATCHING: 3" in result.content[0]["text"]
        # Polygon filters go by POST; the count reuses the same filter.
        first, second = plugin.feature_client.post.call_args_list
        body = first.kwargs["data"]
        assert body["geometryType"] == "esriGeometryPolygon"
        assert body["spatialRel"] == "esriSpatialRelIntersects"
        assert body["inSR"] == 4326 and body["outSR"] == 4326
        assert body["distance"] == 1 and body["units"] == "esriSRUnit_StatuteMile"
        assert json.loads(body["geometry"])["spatialReference"] == {"wkid": 4326}
        assert second.kwargs["data"]["returnCountOnly"] == "true"
        assert second.kwargs["data"]["geometry"] == body["geometry"]

    @pytest.mark.asyncio
    async def test_filter_layer_unions_its_features(self, plugin):
        # GET: filter-layer metadata, then the filter-feature count.
        plugin.feature_client.get = AsyncMock(side_effect=[resp(LAYER_META), count(2)])
        overlapping = {
            "features": [
                {"geometry": esri_square(0, 0, 2, 2)},
                {"geometry": esri_square(1, 1, 3, 3)},
            ]
        }
        # POST: filter features, then the target query and its count.
        plugin.feature_client.post = AsyncMock(
            side_effect=[
                resp(overlapping),
                page([{"APN": "1"}, {"APN": "2"}]),
                count(2),
            ]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_polygon",
                {
                    "item_id": ITEM_ID,
                    "filter_item_id": FILTER_ID,
                    "filter_where": "district = 1",
                    "spatial_rel": "within",
                },
            )
        s = assert_conforms(plugin, "spatial_query_polygon", result)
        assert s["summary"]["filter_features"] == 2
        assert s["summary"]["total_matching"] == 2
        assert s["summary"]["truncated"] is False
        assert s["query"]["filter_where"] == "district = 1"
        assert s["query"]["filter_geometry_type"] is None
        assert s["query"]["distance"] is None and s["query"]["units"] is None
        assert codes(s) == []
        fetch, target, _ = plugin.feature_client.post.call_args_list
        assert fetch.kwargs["data"]["returnGeometry"] == "true"
        assert target.kwargs["data"]["spatialRel"] == "esriSpatialRelWithin"
        # Two overlapping squares become ONE ring: a real union, not a
        # concatenation that would punch a hole where they overlap.
        rings = json.loads(target.kwargs["data"]["geometry"])["rings"]
        assert len(rings) == 1

    @pytest.mark.asyncio
    async def test_oversized_filter_is_generalised_with_a_caveat(
        self, plugin, monkeypatch
    ):
        # A square drawn with 200 collinear points per side: every one of
        # them is redundant, so a 1 m clean collapses it to 4 corners.
        n = 200
        dense = [[-117.2 + 0.1 * i / n, 32.7] for i in range(n)]
        dense += [[-117.1, 32.7 + 0.1 * i / n] for i in range(n)]
        dense += [[-117.1 - 0.1 * i / n, 32.8] for i in range(n)]
        dense += [[-117.2, 32.8 - 0.1 * i / n] for i in range(n)]
        dense.append(dense[0])
        monkeypatch.setattr(ArcGISPlugin, "MAX_FILTER_BYTES", 2000)
        plugin.feature_client.post = AsyncMock(
            side_effect=[page([{"APN": "1"}]), count(1)]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_polygon",
                {
                    "item_id": ITEM_ID,
                    "filter_geometry": {"type": "Polygon", "coordinates": [dense]},
                },
            )
        s = assert_conforms(plugin, "spatial_query_polygon", result)
        assert s["summary"]["filter_simplified_m"] == 1
        assert codes(s) == ["filter_simplified"]
        assert "generalised to a 1 m tolerance" in result.content[0]["text"]
        sent = json.loads(
            plugin.feature_client.post.call_args_list[0].kwargs["data"]["geometry"]
        )
        assert len(sent["rings"][0]) == 5
        assert len(json.dumps(sent, separators=(",", ":"))) <= 2000

    @pytest.mark.asyncio
    async def test_empty(self, plugin):
        plugin.feature_client.post = AsyncMock(side_effect=[page([]), count(0)])
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_polygon", {"item_id": ITEM_ID, "filter_geometry": SQUARE}
            )
        s = assert_conforms(plugin, "spatial_query_polygon", result)
        assert s["rows"] == []
        assert s["summary"]["total_matching"] == 0
        assert codes(s) == ["no_results"]

    @pytest.mark.asyncio
    async def test_count_failure_is_null_not_zero(self, plugin):
        plugin.feature_client.post = AsyncMock(
            side_effect=[
                page([{"APN": "1"}]),
                resp({"error": {"code": 500, "message": "count exploded"}}),
            ]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_polygon", {"item_id": ITEM_ID, "filter_geometry": SQUARE}
            )
        s = assert_conforms(plugin, "spatial_query_polygon", result)
        assert s["rows"] == [{"APN": "1"}]
        assert s["summary"]["total_matching"] is None
        assert s["summary"]["truncated"] is False
        assert codes(s) == ["count_unavailable"]

    @pytest.mark.asyncio
    async def test_missing_filter_is_a_caller_error(self, plugin):
        result = await plugin.execute_tool(
            "spatial_query_polygon", {"item_id": ITEM_ID}
        )
        assert result.success is False
        assert result.structured_content is None
        assert "filter_geometry" in result.error_message
        assert "filter_item_id" in result.error_message

    @pytest.mark.asyncio
    async def test_non_polygon_filter_layer_is_a_caller_error(self, plugin):
        plugin.feature_client.get = AsyncMock(
            return_value=resp({**LAYER_META, "geometryType": "esriGeometryPoint"})
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_polygon",
                {"item_id": ITEM_ID, "filter_item_id": FILTER_ID},
            )
        assert result.success is False
        assert "polygon layer" in result.error_message
        plugin.feature_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_over_cap_filter_refuses_loudly(self, plugin):
        plugin.feature_client.get = AsyncMock(
            side_effect=[resp(LAYER_META), count(ArcGISPlugin.MAX_FILTER_FEATURES + 1)]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_polygon",
                {
                    "item_id": ITEM_ID,
                    "filter_item_id": FILTER_ID,
                    "filter_where": "1=1",
                },
            )
        assert result.success is False
        assert f"max is {ArcGISPlugin.MAX_FILTER_FEATURES:,}" in result.error_message
        plugin.feature_client.post.assert_not_called()


class TestSpatialQueryPoint:
    @pytest.mark.asyncio
    async def test_by_coordinates(self, plugin):
        plugin.feature_client.get = AsyncMock(return_value=page([{"APN": "1"}]))
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_point",
                {"item_id": ITEM_ID, "lon": -117.16, "lat": 32.72},
            )
        s = assert_conforms(plugin, "spatial_query_point", result)
        assert s["rows"][0]["APN"] == "1"
        assert s["summary"]["geocoded"] is False
        assert s["summary"]["snapped_to_meters"] is None
        assert s["summary"]["attribution"] == "SanGIS"
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_by_address_snapped_with_multiple_matches(self, plugin):
        plugin.feature_client.get = AsyncMock(
            side_effect=[
                census(
                    ("202 C ST, SAN DIEGO, CA, 92101", -117.163, 32.717),
                    ("202 C ST, CHULA VISTA, CA, 91910", -117.08, 32.64),
                ),
                page([]),  # exact hit misses (street centerline)
                page([{"APN": "533-1"}]),  # 10 m snap retry
            ]
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_point", {"item_id": ITEM_ID, "address": "202 C St"}
            )
        s = assert_conforms(plugin, "spatial_query_point", result)
        assert s["summary"]["geocoded"] is True
        assert s["summary"]["snapped_to_meters"] == ArcGISPlugin._ADDRESS_SNAP_METERS
        assert s["query"]["matched_address"].startswith("202 C ST, SAN DIEGO")
        assert codes(s) == ["geocoded", "multiple_geocode_matches", "address_snapped"]
        assert "Geocoded '202 C St'" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_by_coordinates_miss_does_not_snap(self, plugin):
        plugin.feature_client.get = AsyncMock(return_value=page([]))
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_point", {"item_id": ITEM_ID, "lon": -117.0, "lat": 33.0}
            )
        s = assert_conforms(plugin, "spatial_query_point", result)
        assert s["rows"] == []
        assert s["summary"]["snapped_to_meters"] is None
        assert codes(s) == ["no_results"]

    @pytest.mark.asyncio
    async def test_address_not_found_is_a_caller_error(self, plugin):
        plugin.feature_client.get = AsyncMock(return_value=census())
        result = await plugin.execute_tool(
            "spatial_query_point", {"item_id": ITEM_ID, "address": "nowhere"}
        )
        assert result.success is False
        assert result.structured_content is None
        assert "Could not geocode" in result.error_message

    @pytest.mark.asyncio
    async def test_limit_clamped_and_truncated(self, plugin):
        plugin.feature_client.get = AsyncMock(
            return_value=page([{"A": i} for i in range(50)])
        )
        with with_dataset(plugin):
            result = await plugin.execute_tool(
                "spatial_query_point",
                {"item_id": ITEM_ID, "lon": -117.0, "lat": 33.0, "limit": 500},
            )
        s = assert_conforms(plugin, "spatial_query_point", result)
        assert s["query"]["limit"] == 50
        assert set(codes(s)) == {"limit_clamped", "results_truncated"}


class TestGeocodeAddress:
    @pytest.mark.asyncio
    async def test_candidates_via_census(self, plugin):
        plugin.feature_client.get = AsyncMock(
            return_value=census(("202 C ST, SAN DIEGO, CA, 92101", -117.163, 32.717))
        )
        result = await plugin.execute_tool("geocode_address", {"address": "202 C St"})
        s = assert_conforms(plugin, "geocode_address", result)
        assert s["candidates"][0]["lon"] == -117.163
        assert s["summary"]["geocoder"] == "census"
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_no_match(self, plugin):
        plugin.feature_client.get = AsyncMock(return_value=census())
        result = await plugin.execute_tool("geocode_address", {"address": "nowhere"})
        s = assert_conforms(plugin, "geocode_address", result)
        assert s["candidates"] == []
        assert codes(s) == ["no_results"]
        assert "San Diego, CA" in result.content[0]["text"]


class TestWire:
    @pytest.mark.asyncio
    async def test_structured_content_reaches_tools_call(self, plugin):
        plugin.portal_client.get = AsyncMock(
            return_value=resp({"results": [PORTAL_ITEM]})
        )
        manager = PluginManager({})
        manager.plugins = {"arcgis": plugin}
        manager.tools = {"arcgis__search_datasets": ("arcgis", "search_datasets")}
        manager._initialized = True
        server = MCPServer(manager)
        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "arcgis__search_datasets",
                    "arguments": {"q": "parcels"},
                },
            }
        )
        result = response["result"]
        assert result["content"][0]["type"] == "text"
        Draft202012Validator(schema_for(plugin, "search_datasets")).validate(
            result["structuredContent"]
        )
