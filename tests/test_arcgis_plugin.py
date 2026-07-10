"""Comprehensive tests for the ArcGIS Enterprise portal plugin.

These tests verify plugin initialization, tool execution, API interactions,
error handling, and data formatting. Tests are designed to fail if functionality breaks.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pydantic import ValidationError

from core.interfaces import PluginType
from plugins.arcgis.config_schema import ArcGISPluginConfig
from plugins.arcgis.plugin import ArcGISPlugin
from plugins.arcgis.where_validator import WhereValidator


@pytest.fixture
def arcgis_config():
    """Standard ArcGIS Enterprise plugin configuration."""
    return {
        "portal_url": "https://geo.example.gov/portal",
        "services_url": "https://geo.example.gov/server/rest/services",
        "city_name": "TestCity",
        "timeout": 120,
    }


def _resp(payload, status_code=200):
    r = Mock()
    r.status_code = status_code
    r.raise_for_status = Mock()
    r.json.return_value = payload
    return r


# ── Plugin attributes ──────────────────────────────────────────────────


class TestPluginAttributes:
    def test_plugin_attributes(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        assert plugin.plugin_name == "arcgis"
        assert plugin.plugin_type == PluginType.OPEN_DATA


# ── Initialization ─────────────────────────────────────────────────────


class TestInitialization:
    @pytest.mark.asyncio
    async def test_initialize_success_portal_mode(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=_resp({"results": []}))
            mock_client_class.return_value = mock_client

            result = await plugin.initialize()

            assert result is True
            assert plugin._initialized is True
            assert plugin._search_mode == "portal"

    @pytest.mark.asyncio
    async def test_initialize_falls_back_to_directory(self, arcgis_config):
        """Portal search answering with an error body (not anonymous) must
        fall discovery back to the services-directory walk."""
        plugin = ArcGISPlugin(arcgis_config)

        gated = _resp({"error": {"code": 499, "message": "Token Required"}})
        directory = _resp({"folders": [], "services": []})

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[gated, directory])
            mock_client_class.return_value = mock_client

            result = await plugin.initialize()

            assert result is True
            assert plugin._search_mode == "directory"

    @pytest.mark.asyncio
    async def test_initialize_failure(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client_class.return_value = mock_client

            result = await plugin.initialize()

            assert result is False
            assert plugin._initialized is False


# ── get_tools ──────────────────────────────────────────────────────────


class TestGetTools:
    def test_get_tools_returns_expected_tools(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        tools = plugin.get_tools()

        tool_names = {t.name for t in tools}
        assert tool_names == {
            "search_datasets",
            "get_dataset",
            "get_aggregations",
            "query_data",
            "get_layer_schema",
            "get_distinct_values",
            "spatial_query_point",
            "geocode_address",
        }


# ── execute_tool ───────────────────────────────────────────────────────


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_execute_tool_unknown(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        result = await plugin.execute_tool("unknown_tool", {})

        assert result.success is False
        assert "Unknown tool" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_tool_search_datasets(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        with patch.object(
            plugin,
            "search_datasets",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": "abc123",
                    "title": "Test Dataset",
                    "tags": [],
                    "description": "desc",
                }
            ],
        ):
            result = await plugin.execute_tool("search_datasets", {"q": "test"})

        assert result.success is True
        assert len(result.content) > 0
        assert "text" in result.content[0]

    @pytest.mark.asyncio
    async def test_execute_tool_get_dataset(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Test",
                "tags": [],
                "description": "desc",
                "service_url": "https://example.com/FeatureServer/0",
            },
        ):
            result = await plugin.execute_tool("get_dataset", {"dataset_id": "abc123"})

        assert result.success is True
        assert len(result.content) > 0

    @pytest.mark.asyncio
    async def test_execute_tool_query_data(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        with patch.object(
            plugin,
            "query_data",
            new_callable=AsyncMock,
            return_value=[{"name": "Park A", "status": "Open"}],
        ):
            result = await plugin.execute_tool("query_data", {"dataset_id": "abc123"})

        assert result.success is True
        assert len(result.content) > 0

    @pytest.mark.asyncio
    async def test_execute_tool_get_aggregations(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        with patch.object(
            plugin,
            "get_aggregations",
            new_callable=AsyncMock,
            return_value=[
                {"key": "Feature Layer", "doc_count": 42},
                {"key": "Table", "doc_count": 10},
            ],
        ):
            result = await plugin.execute_tool("get_aggregations", {"field": "type"})

        assert result.success is True
        assert "Feature Layer" in result.content[0]["text"]


# ── search_datasets type filter ───────────────────────────────────────


class TestSearchDatasetsTypeFilter:
    @staticmethod
    def _plugin_capturing_params(arcgis_config):
        """Plugin whose portal_client records the params of the search request."""
        plugin = ArcGISPlugin(arcgis_config)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_resp({"results": []}))
        plugin.portal_client = mock_client
        return plugin, mock_client

    @pytest.mark.asyncio
    async def test_type_filter_added_to_query(self, arcgis_config):
        plugin, mock_client = self._plugin_capturing_params(arcgis_config)
        await plugin.search_datasets("election", 20, "Feature Service")
        params = mock_client.get.call_args.kwargs["params"]
        assert params["q"] == 'election AND type:"Feature Service"'
        assert params["num"] == 20
        assert params["f"] == "json"

    @pytest.mark.asyncio
    async def test_no_type_filter_uses_bare_query(self, arcgis_config):
        plugin, mock_client = self._plugin_capturing_params(arcgis_config)
        await plugin.search_datasets("parks", 10)
        params = mock_client.get.call_args.kwargs["params"]
        assert params["q"] == "parks"

    @pytest.mark.asyncio
    async def test_type_filter_strips_double_quotes(self, arcgis_config):
        # Defend the quoted phrase against injection / breakage.
        plugin, mock_client = self._plugin_capturing_params(arcgis_config)
        await plugin.search_datasets("x", 10, 'Weird"Type')
        params = mock_client.get.call_args.kwargs["params"]
        assert params["q"] == 'x AND type:"WeirdType"'

    @pytest.mark.asyncio
    async def test_search_targets_sharing_rest(self, arcgis_config):
        plugin, mock_client = self._plugin_capturing_params(arcgis_config)
        await plugin.search_datasets("parks", 10)
        assert mock_client.get.call_args[0][0] == "/sharing/rest/search"

    @pytest.mark.asyncio
    async def test_search_raises_on_error_body(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            return_value=_resp({"error": {"code": 400, "message": "bad q"}})
        )
        plugin.portal_client = mock_client
        with pytest.raises(RuntimeError, match="bad q"):
            await plugin.search_datasets("parks", 10)

    @pytest.mark.asyncio
    async def test_execute_tool_passes_type_through(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        with patch.object(
            plugin, "search_datasets", new_callable=AsyncMock, return_value=[]
        ) as mock_search:
            await plugin.execute_tool(
                "search_datasets", {"q": "election", "type": "Feature Service"}
            )
        mock_search.assert_awaited_once_with("election", 10, "Feature Service")


# ── directory-walk fallback discovery ─────────────────────────────────


class TestDirectorySearch:
    @staticmethod
    def _plugin(arcgis_config, responses):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        plugin._search_mode = "directory"
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[_resp(p) for p in responses])
        plugin.feature_client = mock_client
        return plugin, mock_client

    _ROOT = {
        "folders": ["Hosted", "GeoDepot"],
        "services": [{"name": "Bike_Lockers", "type": "FeatureServer"}],
    }
    _HOSTED = {
        "services": [
            {"name": "Hosted/Parcels", "type": "FeatureServer"},
            {"name": "Hosted/Parcels_Map", "type": "MapServer"},
            {"name": "Hosted/Floodplain", "type": "FeatureServer"},
        ]
    }
    _GATED = {"error": {"code": 499, "message": "Token Required"}}

    @pytest.mark.asyncio
    async def test_directory_search_matches_and_skips_gated_folder(self, arcgis_config):
        plugin, mock_client = self._plugin(
            arcgis_config, [self._ROOT, self._HOSTED, self._GATED]
        )
        results = await plugin.search_datasets("parcels", 10)
        # GeoDepot's token error was skipped, not fatal; both parcel
        # services (Feature + Map) matched.
        ids = [r["id"] for r in results]
        assert "Hosted/Parcels/FeatureServer" in ids
        assert "Hosted/Parcels_Map/MapServer" in ids
        assert all("Floodplain" not in i for i in ids)
        assert mock_client.get.call_count == 3

    @pytest.mark.asyncio
    async def test_directory_search_type_filter(self, arcgis_config):
        plugin, _ = self._plugin(arcgis_config, [self._ROOT, self._HOSTED, self._GATED])
        results = await plugin.search_datasets("parcels", 10, "Feature Service")
        assert [r["id"] for r in results] == ["Hosted/Parcels/FeatureServer"]
        assert results[0]["type"] == "Feature Service"
        assert results[0]["title"] == "Parcels"

    @pytest.mark.asyncio
    async def test_directory_listing_is_cached(self, arcgis_config):
        plugin, mock_client = self._plugin(
            arcgis_config, [self._ROOT, self._HOSTED, self._GATED]
        )
        await plugin.search_datasets("parcels", 10)
        await plugin.search_datasets("floodplain", 10)  # served from cache
        assert mock_client.get.call_count == 3


# ── schema / distinct values / spatial point ──────────────────────────


class TestSchemaDistinctSpatial:
    @staticmethod
    def _plugin(arcgis_config, meta=None, query_payload=None):
        """Plugin with get_dataset stubbed to a layer-bearing service and a
        feature_client that returns `meta` for ?f=json and `query_payload`
        for /query calls."""
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        def make_response(payload):
            resp = Mock()
            resp.status_code = 200
            resp.raise_for_status = Mock()
            resp.json.return_value = payload
            return resp

        async def fake_get(url, params=None):
            params = params or {}
            if params.get("f") == "json" and not url.endswith("/query"):
                return make_response(meta or {})
            return make_response(query_payload or {"features": []})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=fake_get)
        plugin.feature_client = mock_client

        # _layer_url_for_item -> get_dataset resolves to a layer at index 1.
        async def fake_dataset(item_id):
            return {
                "id": item_id,
                "service_url": "https://services1.arcgis.com/x/Parcels/FeatureServer/1",
            }

        plugin.get_dataset = fake_dataset
        return plugin, mock_client

    @pytest.mark.asyncio
    async def test_get_layer_schema_lists_fields(self, arcgis_config):
        meta = {
            "name": "Parcels",
            "geometryType": "esriGeometryPolygon",
            "fields": [
                {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"},
                {
                    "name": "MAP_PAR_ID",
                    "type": "esriFieldTypeString",
                    "alias": "Map ID",
                },
            ],
        }
        plugin, _ = self._plugin(arcgis_config, meta=meta)
        schema = await plugin.get_layer_schema("abc")
        names = [f["name"] for f in schema["fields"]]
        assert names == ["OBJECTID", "MAP_PAR_ID"]
        assert schema["geometry_type"] == "esriGeometryPolygon"

    @pytest.mark.asyncio
    async def test_get_layer_schema_keyword_filters(self, arcgis_config):
        meta = {
            "name": "Parcels",
            "fields": [
                {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"},
                {
                    "name": "MAP_PAR_ID",
                    "type": "esriFieldTypeString",
                    "alias": "Map ID",
                },
            ],
        }
        plugin, _ = self._plugin(arcgis_config, meta=meta)
        schema = await plugin.get_layer_schema("abc", keyword="map")
        assert [f["name"] for f in schema["fields"]] == ["MAP_PAR_ID"]

    @pytest.mark.asyncio
    async def test_get_distinct_values_extracts_and_sets_params(self, arcgis_config):
        payload = {
            "features": [
                {"attributes": {"Record_Status": "Active"}},
                {"attributes": {"Record_Status": "Complete"}},
            ]
        }
        plugin, mock_client = self._plugin(arcgis_config, query_payload=payload)
        values = await plugin.get_distinct_values("abc", "Record_Status")
        assert values == ["Active", "Complete"]
        params = mock_client.get.call_args.kwargs["params"]
        assert params["returnDistinctValues"] == "true"
        assert params["outFields"] == "Record_Status"
        assert params["orderByFields"] == "Record_Status"

    @pytest.mark.asyncio
    async def test_get_distinct_values_like_builds_clause(self, arcgis_config):
        plugin, mock_client = self._plugin(
            arcgis_config, query_payload={"features": []}
        )
        await plugin.get_distinct_values("abc", "Permit_For", like="ADU")
        params = mock_client.get.call_args.kwargs["params"]
        assert params["where"] == "Permit_For LIKE '%ADU%'"

    @pytest.mark.asyncio
    async def test_spatial_query_point_builds_geometry_params(self, arcgis_config):
        payload = {"features": [{"attributes": {"WARD": "5"}}]}
        plugin, mock_client = self._plugin(arcgis_config, query_payload=payload)
        records = await plugin.spatial_query_point("abc", -71.8, 42.26)
        assert records == [{"WARD": "5"}]
        params = mock_client.get.call_args.kwargs["params"]
        assert params["geometry"] == "-71.8,42.26"
        assert params["geometryType"] == "esriGeometryPoint"
        assert params["spatialRel"] == "esriSpatialRelIntersects"
        assert params["inSR"] == 4326

    @pytest.mark.asyncio
    async def test_spatial_query_point_rejects_bad_coords(self, arcgis_config):
        plugin, _ = self._plugin(arcgis_config)
        with pytest.raises(ValueError, match="lon"):
            await plugin.spatial_query_point("abc", -999, 42.26)
        with pytest.raises(ValueError, match="lat"):
            await plugin.spatial_query_point("abc", -71.8, 999)

    @pytest.mark.asyncio
    async def test_no_service_url_raises(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        async def no_url(item_id):
            return {"id": item_id, "service_url": ""}

        plugin.get_dataset = no_url
        with pytest.raises(ValueError, match="queryable Feature Service URL"):
            await plugin.get_layer_schema("abc")


# ── query_data two-hop resolution ─────────────────────────────────────


class TestQueryDataTwoHop:
    @pytest.mark.asyncio
    async def test_query_data_two_hop(self, arcgis_config):
        """Verify query_data calls get_dataset first, then queries the Feature Service."""
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        mock_feature_client = AsyncMock()
        mock_feature_response = Mock()
        mock_feature_response.status_code = 200
        mock_feature_response.raise_for_status = Mock()
        mock_feature_response.json.return_value = {
            "features": [
                {"attributes": {"name": "Park A", "status": "Open"}},
            ]
        }
        mock_feature_client.get = AsyncMock(return_value=mock_feature_response)
        plugin.feature_client = mock_feature_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Parks",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer/0",
            },
        ) as mock_get_dataset:
            records = await plugin.query_data("abc123", {"where": "1=1"}, 100)

        mock_get_dataset.assert_called_once_with("abc123")
        mock_feature_client.get.assert_called_once()
        call_args = mock_feature_client.get.call_args
        assert "/query" in call_args[0][0]
        assert len(records) == 1
        assert records[0]["name"] == "Park A"

    @pytest.mark.asyncio
    async def test_query_data_auto_appends_layer_index(self, arcgis_config):
        """When service_url ends with /FeatureServer (no layer), /0 is appended."""
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        mock_feature_client = AsyncMock()
        mock_feature_response = Mock()
        mock_feature_response.status_code = 200
        mock_feature_response.raise_for_status = Mock()
        mock_feature_response.json.return_value = {
            "features": [{"attributes": {"name": "Skate Park"}}]
        }
        mock_feature_client.get = AsyncMock(return_value=mock_feature_response)
        plugin.feature_client = mock_feature_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Parks",
                "service_url": "https://services.arcgis.com/xyz/FeatureServer",
            },
        ):
            records = await plugin.query_data("abc123", {"where": "1=1"}, 100)

        url_called = mock_feature_client.get.call_args[0][0]
        assert "/FeatureServer/0/query" in url_called
        assert len(records) == 1


# ── Tier 1 polish: clean text, total count, order_by ──────────────────


class TestTier1Polish:
    def test_clean_text_strips_html_and_entities(self):
        out = ArcGISPlugin._clean_text("A<div><br/></div>B&nbsp;&amp; C")
        assert "<" not in out and ">" not in out
        assert "&nbsp;" not in out and "&amp;" not in out
        assert "A" in out and "B" in out and "& C" in out

    def test_clean_text_normalizes_unicode_to_ascii(self):
        out = ArcGISPlugin._clean_text("café — “q” end")
        assert all(ord(c) < 128 for c in out)
        assert "cafe" in out and '"q"' in out and "--" in out

    def test_clean_text_handles_none(self):
        assert ArcGISPlugin._clean_text(None) == ""

    def test_format_query_results_cleans_html_in_values(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        out = plugin._format_query_results([{"desc": "a<br/>b"}], 1)
        assert "<br" not in out and "a b" in out

    def test_format_query_results_shows_total(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        out = plugin._format_query_results([{"x": 1}], 1, total=4242)
        assert "TOTAL MATCHING: 4242" in out
        assert "Returned 1 record(s)" in out

    @pytest.mark.asyncio
    async def test_query_data_passes_order_by(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        resp = Mock()
        resp.status_code = 200
        resp.raise_for_status = Mock()
        resp.json.return_value = {"features": [{"attributes": {"x": 1}}]}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        plugin.feature_client = mock_client
        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={"id": "a", "service_url": "https://s/FeatureServer/1"},
        ):
            await plugin.query_data(
                "a", {"where": "1=1", "order_by": "Date_Submitted DESC"}, 5
            )
        params = mock_client.get.call_args.kwargs["params"]
        assert params["orderByFields"] == "Date_Submitted DESC"

    @pytest.mark.asyncio
    async def test_get_record_count_returns_count(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.json.return_value = {"count": 4242}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        plugin.feature_client = mock_client
        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={"id": "a", "service_url": "https://s/FeatureServer/1"},
        ):
            n = await plugin.get_record_count("a", "1=1")
        assert n == 4242
        assert mock_client.get.call_args.kwargs["params"]["returnCountOnly"] == "true"

    @pytest.mark.asyncio
    async def test_execute_query_data_includes_total(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        with (
            patch.object(
                plugin, "query_data", new_callable=AsyncMock, return_value=[{"n": "A"}]
            ),
            patch.object(
                plugin, "get_record_count", new_callable=AsyncMock, return_value=4242
            ),
        ):
            r = await plugin.execute_tool("query_data", {"dataset_id": "a"})
        assert "TOTAL MATCHING: 4242" in r.content[0]["text"]

    @pytest.mark.asyncio
    async def test_execute_query_data_count_failure_is_nonfatal(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        with (
            patch.object(
                plugin, "query_data", new_callable=AsyncMock, return_value=[{"n": "A"}]
            ),
            patch.object(
                plugin,
                "get_record_count",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
        ):
            r = await plugin.execute_tool("query_data", {"dataset_id": "a"})
        assert r.success is True
        assert "Returned 1 record(s)" in r.content[0]["text"]
        assert "TOTAL MATCHING" not in r.content[0]["text"]


# ── geocoding ─────────────────────────────────────────────────────────


class TestGeocoding:
    @staticmethod
    def _plugin(arcgis_config, payload, region="Worcester, MA"):
        cfg = dict(arcgis_config)
        cfg["geocoder_region"] = region
        plugin = ArcGISPlugin(cfg)
        plugin.plugin_config = ArcGISPluginConfig(**cfg)
        resp = Mock()
        resp.raise_for_status = Mock()
        resp.json.return_value = payload
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        plugin.feature_client = mock_client
        return plugin, mock_client

    _MATCH = {
        "result": {
            "addressMatches": [
                {
                    "matchedAddress": "455 MAIN ST, WORCESTER, MA, 01608",
                    "coordinates": {"x": -71.8021, "y": 42.2634},
                }
            ]
        }
    }

    @pytest.mark.asyncio
    async def test_geocode_returns_lonlat(self, arcgis_config):
        plugin, _ = self._plugin(arcgis_config, self._MATCH)
        out = await plugin.geocode_address("455 Main St")
        assert out == [
            {
                "matched_address": "455 MAIN ST, WORCESTER, MA, 01608",
                "lon": -71.8021,
                "lat": 42.2634,
            }
        ]

    @pytest.mark.asyncio
    async def test_geocode_appends_region(self, arcgis_config):
        plugin, mock_client = self._plugin(arcgis_config, self._MATCH)
        await plugin.geocode_address("455 Main St")
        assert (
            mock_client.get.call_args.kwargs["params"]["address"]
            == "455 Main St, Worcester, MA"
        )

    @pytest.mark.asyncio
    async def test_geocode_skips_region_if_present(self, arcgis_config):
        plugin, mock_client = self._plugin(arcgis_config, self._MATCH)
        await plugin.geocode_address("455 Main St, Worcester, MA")
        assert (
            mock_client.get.call_args.kwargs["params"]["address"]
            == "455 Main St, Worcester, MA"
        )

    @pytest.mark.asyncio
    async def test_geocode_no_match_returns_empty(self, arcgis_config):
        plugin, _ = self._plugin(arcgis_config, {"result": {"addressMatches": []}})
        assert await plugin.geocode_address("nowhere") == []

    @pytest.mark.asyncio
    async def test_spatial_query_point_geocodes_address(self, arcgis_config):
        cfg = dict(arcgis_config)
        cfg["geocoder_region"] = "Worcester, MA"
        plugin = ArcGISPlugin(cfg)
        plugin.plugin_config = ArcGISPluginConfig(**cfg)
        with (
            patch.object(
                plugin,
                "geocode_address",
                new_callable=AsyncMock,
                return_value=[
                    {"matched_address": "455 MAIN ST", "lon": -71.8, "lat": 42.26}
                ],
            ),
            patch.object(
                plugin,
                "spatial_query_point",
                new_callable=AsyncMock,
                return_value=[{"WARD": "5"}],
            ) as mock_spatial,
        ):
            result = await plugin.execute_tool(
                "spatial_query_point", {"item_id": "abc", "address": "455 Main St"}
            )
        assert result.success is True
        # geocoded coords were passed to the spatial query
        assert mock_spatial.call_args[0][1] == -71.8
        assert mock_spatial.call_args[0][2] == 42.26
        assert "Geocoded" in result.content[0]["text"]
        assert "WARD" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_spatial_query_point_unresolvable_address(self, arcgis_config):
        cfg = dict(arcgis_config)
        plugin = ArcGISPlugin(cfg)
        plugin.plugin_config = ArcGISPluginConfig(**cfg)
        with patch.object(
            plugin, "geocode_address", new_callable=AsyncMock, return_value=[]
        ):
            result = await plugin.execute_tool(
                "spatial_query_point", {"item_id": "abc", "address": "nowhere"}
            )
        assert result.success is False
        assert "Could not geocode" in result.error_message

    @pytest.mark.asyncio
    async def test_spatial_query_point_requires_coords_or_address(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        result = await plugin.execute_tool("spatial_query_point", {"item_id": "abc"})
        assert result.success is False
        assert "address" in result.error_message


# ── multi-word fallback + aggregation field validation ───────────────


class TestSearchFallbackAndAggValidation:
    @pytest.mark.asyncio
    async def test_multiword_fallback_to_longest_word(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        empty = _resp({"results": []})
        hit = _resp({"results": [{"id": "x", "title": "Zoning Districts"}]})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[empty, hit])
        plugin.portal_client = mock_client

        results = await plugin.search_datasets("zoning districts map", 10)

        assert len(results) == 1
        assert mock_client.get.call_count == 2
        # fallback used the longest token ("districts")
        assert mock_client.get.call_args_list[1].kwargs["params"]["q"] == "districts"

    @pytest.mark.asyncio
    async def test_no_fallback_when_first_search_hits(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        hit = _resp({"results": [{"id": "x", "title": "T"}]})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=hit)
        plugin.portal_client = mock_client

        results = await plugin.search_datasets("building permits", 10)

        assert len(results) == 1
        assert mock_client.get.call_count == 1  # found first try, no fallback

    @pytest.mark.asyncio
    async def test_no_fallback_for_single_word(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        empty = _resp({"results": []})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=empty)
        plugin.portal_client = mock_client

        results = await plugin.search_datasets("parcels", 10)

        assert results == []
        assert mock_client.get.call_count == 1  # nothing to fall back to

    @pytest.mark.asyncio
    async def test_aggregations_unknown_field_raises_hint(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        with pytest.raises(ValueError, match="not an aggregatable field"):
            await plugin.get_aggregations("source")

    @pytest.mark.asyncio
    async def test_aggregations_tally_search_results(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        items = [
            {"id": "a", "type": "Feature Service", "tags": ["parcels", "sandag"]},
            {"id": "b", "type": "Feature Service", "tags": ["sandag"]},
            {"id": "c", "type": "Web Map", "tags": []},
        ]
        with patch.object(
            plugin, "_search_items", new_callable=AsyncMock, return_value=items
        ) as mock_search:
            buckets = await plugin.get_aggregations("type", q="parcels")
        mock_search.assert_awaited_once_with("parcels", 100, None)
        assert buckets == [
            {"key": "Feature Service", "doc_count": 2},
            {"key": "Web Map", "doc_count": 1},
        ]

    @pytest.mark.asyncio
    async def test_aggregations_tally_list_field(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        items = [
            {"id": "a", "tags": ["parcels", "sandag"]},
            {"id": "b", "tags": ["sandag"]},
        ]
        with patch.object(
            plugin, "_search_items", new_callable=AsyncMock, return_value=items
        ):
            buckets = await plugin.get_aggregations("tags")
        assert buckets[0] == {"key": "sandag", "doc_count": 2}


# ── Layer URL helper ───────────────────────────────────────────────────


class TestEnsureLayerUrl:
    @staticmethod
    def _plugin_with_meta(arcgis_config, meta):
        """Plugin whose feature_client returns the given service metadata."""
        plugin = ArcGISPlugin(arcgis_config)
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = meta
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        plugin.feature_client = mock_client
        return plugin

    @pytest.mark.asyncio
    async def test_resolves_first_layer_id_from_metadata(self, arcgis_config):
        # Service publishes its only layer at a non-zero index (MassGIS-style).
        plugin = self._plugin_with_meta(
            arcgis_config, {"layers": [{"id": 1, "name": "Parcel Polygons"}]}
        )
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/1"

    @pytest.mark.asyncio
    async def test_falls_back_to_tables_when_no_layers(self, arcgis_config):
        plugin = self._plugin_with_meta(
            arcgis_config,
            {"layers": [], "tables": [{"id": 2, "name": "Assessing"}]},
        )
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/2"

    @pytest.mark.asyncio
    async def test_defaults_to_layer_zero_when_metadata_unavailable(
        self, arcgis_config
    ):
        plugin = ArcGISPlugin(arcgis_config)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        plugin.feature_client = mock_client
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/0"

    @pytest.mark.asyncio
    async def test_preserves_existing_layer_index(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.feature_client = AsyncMock()  # must not be consulted
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer/3"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/3"
        plugin.feature_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_map_server(self, arcgis_config):
        plugin = self._plugin_with_meta(
            arcgis_config, {"layers": [{"id": 0, "name": "Base"}]}
        )
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/MapServer"
        )
        assert result == "https://services.arcgis.com/xyz/MapServer/0"

    @pytest.mark.asyncio
    async def test_strips_trailing_slash(self, arcgis_config):
        plugin = self._plugin_with_meta(
            arcgis_config, {"layers": [{"id": 0, "name": "Base"}]}
        )
        result = await plugin._ensure_layer_url(
            "https://services.arcgis.com/xyz/FeatureServer/"
        )
        assert result == "https://services.arcgis.com/xyz/FeatureServer/0"

    @pytest.mark.asyncio
    async def test_query_data_uses_non_zero_layer_index(self, arcgis_config):
        """Regression: a service whose only layer is at index 1 (e.g. the
        Worcester Parcel Polygons service) must be queried at /1, not /0."""
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        meta_response = Mock()
        meta_response.raise_for_status = Mock()
        meta_response.json.return_value = {
            "layers": [{"id": 1, "name": "Parcel Polygons"}]
        }
        query_response = Mock()
        query_response.status_code = 200
        query_response.raise_for_status = Mock()
        query_response.json.return_value = {
            "features": [{"attributes": {"MAP_PAR_ID": "12-345"}}]
        }

        mock_feature_client = AsyncMock()
        mock_feature_client.get = AsyncMock(side_effect=[meta_response, query_response])
        plugin.feature_client = mock_feature_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc",
                "title": "Parcels",
                "service_url": (
                    "https://services.arcgis.com/xyz/Parcel_Polygons/FeatureServer"
                ),
            },
        ):
            records = await plugin.query_data("abc", {"where": "1=1"}, 10)

        meta_url = mock_feature_client.get.call_args_list[0][0][0]
        query_url = mock_feature_client.get.call_args_list[1][0][0]
        assert meta_url.endswith("/FeatureServer")
        assert "/FeatureServer/1/query" in query_url
        assert records == [{"MAP_PAR_ID": "12-345"}]


# ── WhereValidator ─────────────────────────────────────────────────────


class TestWhereValidator:
    def test_where_validator_blocks_delete(self):
        with pytest.raises(ValueError, match="DELETE"):
            WhereValidator.validate("DELETE FROM x")

    def test_where_validator_allows_valid(self):
        result = WhereValidator.validate("status = 'Active'")
        assert result == "status = 'Active'"

    def test_where_validator_empty(self):
        result = WhereValidator.validate("")
        assert result == "1=1"

    def test_where_validator_does_not_flag_deleted_at(self):
        result = WhereValidator.validate("deleted_at IS NULL")
        assert result == "deleted_at IS NULL"


class TestWhereValidatorAgainstSchema:
    def test_default_where_skipped(self):
        WhereValidator.validate_against_schema("1=1", {"STATUS"})
        WhereValidator.validate_against_schema("", {"STATUS"})

    def test_empty_allowed_fields_skips(self):
        WhereValidator.validate_against_schema("FOO='x'", None)
        WhereValidator.validate_against_schema("FOO='x'", set())

    def test_valid_field_passes(self):
        WhereValidator.validate_against_schema("STATUS='Active'", {"STATUS", "NAME"})

    def test_typo_field_raises_with_suggestion(self):
        with pytest.raises(ValueError, match="STATUUS"):
            WhereValidator.validate_against_schema(
                "STATUUS='Active'", {"STATUS", "NAME"}
            )

    def test_typo_field_includes_closest_match(self):
        try:
            WhereValidator.validate_against_schema(
                "STATUUS='Active'", {"STATUS", "NAME"}
            )
        except ValueError as e:
            assert "STATUS" in str(e)
            assert "did you mean" in str(e).lower()

    def test_unknown_field_no_close_match(self):
        with pytest.raises(ValueError, match="XYZQR"):
            WhereValidator.validate_against_schema("XYZQR='foo'", {"STATUS", "NAME"})

    def test_sql_keywords_not_treated_as_fields(self):
        # IS, NULL, AND, OR, IN, LIKE — all SQL keywords, not fields.
        WhereValidator.validate_against_schema(
            "STATUS IS NULL AND NAME LIKE 'A%'", {"STATUS", "NAME"}
        )

    def test_case_sensitive_field_match(self):
        # ArcGIS field names are case-sensitive. STATUS != status.
        with pytest.raises(ValueError, match="status"):
            WhereValidator.validate_against_schema("status='Active'", {"STATUS"})

    def test_string_values_not_treated_as_fields(self):
        # The literal 'Park' must not be flagged as an unknown field.
        WhereValidator.validate_against_schema("NAME='Park'", {"NAME"})

    def test_function_call_field_inside(self):
        # UPPER is a keyword; STATUS inside is the real field.
        WhereValidator.validate_against_schema("UPPER(STATUS)='ACTIVE'", {"STATUS"})


# ── Config schema ──────────────────────────────────────────────────────


class TestConfigSchema:
    def test_config_schema_valid(self):
        config = ArcGISPluginConfig(
            portal_url="https://geo.example.gov/portal",
            services_url="https://geo.example.gov/server/rest/services",
            city_name="San Diego",
            timeout=60,
        )
        assert config.city_name == "San Diego"
        assert config.portal_url == "https://geo.example.gov/portal"
        assert config.services_url == "https://geo.example.gov/server/rest/services"
        assert config.timeout == 60
        assert config.token is None
        assert config.geocoder_url == ""

    def test_config_schema_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            ArcGISPluginConfig(
                portal_url="https://geo.example.gov/portal",
                city_name="San Diego",
                unknown_field="oops",
            )

    def test_config_schema_strips_trailing_slash(self):
        config = ArcGISPluginConfig(
            portal_url="https://geo.example.gov/portal/",
            city_name="San Diego",
        )
        assert config.portal_url == "https://geo.example.gov/portal"

    def test_config_schema_rejects_invalid_url(self):
        with pytest.raises(ValidationError):
            ArcGISPluginConfig(
                portal_url="not-a-url",
                city_name="San Diego",
            )

    def test_config_schema_requires_a_discovery_endpoint(self):
        with pytest.raises(ValidationError, match="portal_url or services_url"):
            ArcGISPluginConfig(city_name="San Diego")

    def test_config_schema_services_url_only_is_valid(self):
        config = ArcGISPluginConfig(
            services_url="https://geo.example.gov/server/rest/services",
            city_name="San Diego",
        )
        assert config.portal_url == ""


# ── get_dataset ID forms, caching, and attribution ─────────────────────


class TestGetDataset:
    _ITEM_ID = "032a5dcf654c4ccbb18711ad8a0ee754"

    _PORTAL_ITEM = {
        "id": _ITEM_ID,
        "title": "Parcels",
        "type": "Feature Service",
        "url": "https://geo.example.gov/server/rest/services/Hosted/Parcels/FeatureServer",
        "access": "public",
        "owner": "SanGIS",
        "tags": ["parcels"],
        "accessInformation": "SanGIS using legal recorded data",
        "licenseInfo": "x" * 1000,
    }

    @staticmethod
    def _plugin(arcgis_config, portal_payload=None, feature_payload=None):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        portal_client = AsyncMock()
        portal_client.get = AsyncMock(return_value=_resp(portal_payload or {}))
        plugin.portal_client = portal_client
        feature_client = AsyncMock()
        feature_client.get = AsyncMock(return_value=_resp(feature_payload or {}))
        plugin.feature_client = feature_client
        return plugin, portal_client, feature_client

    @pytest.mark.asyncio
    async def test_portal_item_id_uses_content_endpoint(self, arcgis_config):
        plugin, portal_client, _ = self._plugin(
            arcgis_config, portal_payload=self._PORTAL_ITEM
        )
        dataset = await plugin.get_dataset(self._ITEM_ID)
        assert (
            portal_client.get.call_args[0][0]
            == f"/sharing/rest/content/items/{self._ITEM_ID}"
        )
        assert dataset["service_url"].endswith("/Hosted/Parcels/FeatureServer")
        assert dataset["attribution"] == "SanGIS using legal recorded data"
        # The multi-page EULA is excerpted, not inlined wholesale.
        assert len(dataset["licenseInfo"]) <= 303

    @pytest.mark.asyncio
    async def test_service_path_id_uses_services_directory(self, arcgis_config):
        meta = {
            "serviceDescription": "Tax parcels",
            "copyrightText": "SanGIS attribution",
            "layers": [{"id": 0, "name": "Parcels"}],
        }
        plugin, _, feature_client = self._plugin(arcgis_config, feature_payload=meta)
        dataset = await plugin.get_dataset("Hosted/Parcels/FeatureServer")
        assert feature_client.get.call_args[0][0] == (
            "https://geo.example.gov/server/rest/services/Hosted/Parcels/FeatureServer"
        )
        assert dataset["title"] == "Parcels"
        assert dataset["type"] == "Feature Service"
        assert dataset["attribution"] == "SanGIS attribution"
        assert dataset["service_url"].endswith("/Hosted/Parcels/FeatureServer")

    @pytest.mark.asyncio
    async def test_invalid_dataset_id_rejected(self, arcgis_config):
        plugin, portal_client, feature_client = self._plugin(arcgis_config)
        with pytest.raises(ValueError, match="Invalid dataset ID"):
            await plugin.get_dataset("https://evil.example.com/FeatureServer")
        with pytest.raises(ValueError, match="Invalid dataset ID"):
            await plugin.get_dataset("../../secrets/FeatureServer")
        portal_client.get.assert_not_called()
        feature_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_dataset_is_cached(self, arcgis_config):
        plugin, portal_client, _ = self._plugin(
            arcgis_config, portal_payload=self._PORTAL_ITEM
        )
        first = await plugin.get_dataset(self._ITEM_ID)
        second = await plugin.get_dataset(self._ITEM_ID)
        assert portal_client.get.call_count == 1
        assert first is second

    @pytest.mark.asyncio
    async def test_portal_item_error_body_raises(self, arcgis_config):
        plugin, _, _ = self._plugin(
            arcgis_config,
            portal_payload={"error": {"code": 400, "message": "Item not found"}},
        )
        with pytest.raises(RuntimeError, match="Item not found"):
            await plugin.get_dataset(self._ITEM_ID)


# ── query_data pagination and attribution passthrough ──────────────────


class TestPaginationAndAttribution:
    @pytest.mark.asyncio
    async def test_query_data_paginates_with_result_offset(self, arcgis_config):
        """A layer whose MaxRecordCount is below the requested limit must be
        paged with resultOffset until the limit is satisfied."""
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)

        page1 = _resp(
            {
                "features": [{"attributes": {"i": n}} for n in range(2)],
                "exceededTransferLimit": True,
            }
        )
        page2 = _resp({"features": [{"attributes": {"i": 2}}]})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[page1, page2])
        plugin.feature_client = mock_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={"id": "a", "service_url": "https://s/FeatureServer/0"},
        ):
            records = await plugin.query_data("a", {"where": "1=1"}, 5)

        assert [r["i"] for r in records] == [0, 1, 2]
        first_params = mock_client.get.call_args_list[0].kwargs["params"]
        second_params = mock_client.get.call_args_list[1].kwargs["params"]
        assert "resultOffset" not in first_params
        assert second_params["resultOffset"] == 2
        assert second_params["resultRecordCount"] == 3

    @pytest.mark.asyncio
    async def test_query_params_pin_out_sr_wgs84(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_resp({"features": []}))
        plugin.feature_client = mock_client
        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={"id": "a", "service_url": "https://s/FeatureServer/0"},
        ):
            await plugin.query_data("a", {"where": "1=1"}, 5)
        assert mock_client.get.call_args.kwargs["params"]["outSR"] == 4326

    @pytest.mark.asyncio
    async def test_execute_query_data_appends_attribution(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        with (
            patch.object(
                plugin, "query_data", new_callable=AsyncMock, return_value=[{"n": "A"}]
            ),
            patch.object(
                plugin, "get_record_count", new_callable=AsyncMock, return_value=1
            ),
            patch.object(
                plugin,
                "get_dataset",
                new_callable=AsyncMock,
                return_value={"id": "a", "attribution": "SanGIS"},
            ),
        ):
            r = await plugin.execute_tool("query_data", {"dataset_id": "a"})
        assert "Data attribution: SanGIS" in r.content[0]["text"]

    @pytest.mark.asyncio
    async def test_attribution_failure_is_nonfatal(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        with (
            patch.object(
                plugin, "query_data", new_callable=AsyncMock, return_value=[{"n": "A"}]
            ),
            patch.object(
                plugin, "get_record_count", new_callable=AsyncMock, return_value=1
            ),
            patch.object(
                plugin,
                "get_dataset",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
        ):
            r = await plugin.execute_tool("query_data", {"dataset_id": "a"})
        assert r.success is True
        assert "Data attribution" not in r.content[0]["text"]


# ── ArcGIS GeocodeServer path ──────────────────────────────────────────


class TestArcGISGeocoder:
    @staticmethod
    def _plugin(arcgis_config, payload):
        cfg = dict(arcgis_config)
        cfg["geocoder_url"] = (
            "https://gis.example.gov/rest/services/LOCATOR/GeocodeServer"
        )
        plugin = ArcGISPlugin(cfg)
        plugin.plugin_config = ArcGISPluginConfig(**cfg)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_resp(payload))
        plugin.feature_client = mock_client
        return plugin, mock_client

    _CANDIDATES = {
        "candidates": [
            {
                "address": "202 C ST, San Diego, CA, 92101",
                "location": {"x": -117.1626, "y": 32.7170},
                "score": 100,
            },
            {
                "address": "Somewhere vague",
                "location": {"x": -117.0, "y": 32.0},
                "score": 40,
            },
        ]
    }

    @pytest.mark.asyncio
    async def test_uses_find_address_candidates(self, arcgis_config):
        plugin, mock_client = self._plugin(arcgis_config, self._CANDIDATES)
        out = await plugin.geocode_address("202 C St")
        url = mock_client.get.call_args[0][0]
        assert url.endswith("/GeocodeServer/findAddressCandidates")
        params = mock_client.get.call_args.kwargs["params"]
        assert params["SingleLine"] == "202 C St"
        assert params["outSR"] == 4326
        # weak candidates (score < 60) are dropped
        assert out == [
            {
                "matched_address": "202 C ST, San Diego, CA, 92101",
                "lon": -117.1626,
                "lat": 32.7170,
            }
        ]

    @pytest.mark.asyncio
    async def test_geocoder_error_body_raises(self, arcgis_config):
        plugin, _ = self._plugin(
            arcgis_config, {"error": {"code": 400, "message": "bad address"}}
        )
        with pytest.raises(RuntimeError, match="bad address"):
            await plugin.geocode_address("202 C St")
