# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**San Diego fork.** This is a San Diego fork of the OpenContext MCP server framework ("San Diego GIS MCP"). It serves SANDAG/SanGIS regional GIS data from SANDAG's ArcGIS Enterprise portal (`geo.sandag.org`) via the built-in `arcgis` plugin (see `config.yaml`). It was adapted from the Worcester, MA fork, which targeted an ArcGIS Hub site.

## Data source: SANDAG/SanGIS ArcGIS Enterprise

Facts verified against the live services (keep these in mind when touching discovery or query code):

- **Services directory:** `https://geo.sandag.org/server/rest/services` — a standard ArcGIS Enterprise services directory. Directory-walking with `?f=json` works anonymously; the `Hosted/` folder is public (Library, Floodplain, parcels, etc.).
- **Auth-gated folders:** some folders (e.g. `GeoDepot`) require sign-in. Discovery must skip them and handle 401/403/sign-in responses gracefully rather than failing the walk.
- **Catalog search:** try `https://geo.sandag.org/portal/sharing/rest/search` anonymously first (same API shape as an ArcGIS Online org search). If it is open, use it; if not, fall back to walking the services directory.
- **Feature queries:** standard `/FeatureServer/<N>/query`. `MaxRecordCount` is 2000 — paginate with `resultOffset`.
- **Spatial reference:** layers are stored in EPSG:2230 (CA State Plane Zone VI, US feet). Always request `outSR=4326` on queries, and declare `inSR=4326` on point/geometry inputs.
- **Attribution:** pass each service's `copyrightText` through in tool responses (SanGIS attribution), and link SANDAG's GIS Data Disclaimer in the README.
- **Geocoder (optional):** `SANDAG_COMPOSITE_LOCATOR` GeocodeServer at `https://gis.sandag.org/sdgis/rest/services` (`findAddressCandidates`).

## Build & Development Commands

```bash
# Install dependencies (uv preferred, pip fallback)
uv sync                              # or: pip install -r requirements.txt

# Run local MCP server (no Lambda needed)
python3 scripts/local_server.py      # Serves on http://localhost:8000/mcp
# Or: python3 local_server.py        # Alternate entry point, serves on / and /mcp

# Validate config
python3 -c "from core.validators import load_and_validate_config; load_and_validate_config('config.yaml')"

# Tests
uv run pytest tests/ -n auto                                    # All tests, parallel
uv run pytest tests/test_ckan_plugin.py -v                      # Single file
uv run pytest tests/test_ckan_plugin.py::TestClass::test_name -v  # Single test
uv run pytest tests/ --cov=core --cov=plugins --cov-report=term-missing  # With coverage (80% minimum)

# Linting (ruff)
uv run ruff check core/ plugins/ server/ tests/      # Check
uv run ruff check core/ plugins/ server/ tests/ --fix # Auto-fix
uv run ruff format core/ plugins/ server/ tests/      # Format

# Pre-commit hooks
pre-commit run --all-files

# Go client (requires Go 1.21+)
cd client && make build

# Deploy to AWS
./scripts/deploy.sh --environment staging
```

## Architecture

**Core rule: One Fork = One MCP Server.** Each deployment runs exactly ONE plugin. This is enforced at config validation time (`core/validators.py`) and at runtime (`PluginManager.load_plugins()`). To deploy multiple MCP servers, fork the repo per plugin.

**Request flow:**
```
Claude (stdio) → Go client (client/) or stdio_bridge.py → HTTP POST /mcp
  → Lambda (server/adapters/aws_lambda.py) or local_server.py
  → server/http_handler.py → core/mcp_server.py (JSON-RPC 2.0)
  → core/plugin_manager.py → Plugin → External API
```

**Key modules:**
- `core/interfaces.py` — Abstract bases: `MCPPlugin`, `DataPlugin`, plus `ToolDefinition`, `ToolResult`, `PluginType` enum
- `core/plugin_manager.py` — Discovers plugins by scanning `plugins/` and `custom_plugins/` for `plugin.py` files. Registers tools with `pluginname__toolname` prefix. Routes `tools/call` to the correct plugin.
- `core/mcp_server.py` — Handles MCP JSON-RPC methods: `initialize`, `tools/list`, `tools/call`, `ping`
- `core/validators.py` — Loads config from `config.yaml` (local) or `OPENCONTEXT_CONFIG` env var (Lambda). Enforces single-plugin rule.
- `server/adapters/aws_lambda.py` — AWS Lambda entry point (handler: `server.adapters.aws_lambda.lambda_handler`). Also `server/lambda_handler.py` as legacy entry point.
- `server/http_handler.py` — Cloud-agnostic HTTP handler shared by Lambda and local server
- `stdio_bridge.py` — Python stdio-to-HTTP bridge for connecting Claude Desktop/Code to the local server (alternative to Go client)

**Built-in plugins** (`plugins/`): `ckan`, `arcgis`, `socrata` — each implements `DataPlugin` with `search_datasets`, `get_dataset`, `query_data`. Custom plugins go in `custom_plugins/` and are auto-discovered.

## Plugin Development

New plugins must implement `MCPPlugin` (or `DataPlugin` for data sources). Place in `custom_plugins/<name>/plugin.py`. The class must define `plugin_name`, `plugin_type`, `plugin_version` and implement `initialize()`, `shutdown()`, `get_tools()`, `execute_tool()`, `health_check()`. Tool names are auto-prefixed — return bare names from `get_tools()`.

## Configuration

Copy `config-example.yaml` to `config.yaml`. Enable exactly one plugin. Config supports `${ENV_VAR}` substitution. For Lambda, config is serialized to the `OPENCONTEXT_CONFIG` env var by Terraform.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs ruff lint/format, pip-audit, pytest with coverage, and Go tests on push to main/develop and on PRs.
