# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**San Diego fork.** This is a San Diego fork of the OpenContext MCP server framework ("San Diego GIS MCP"). It serves SANDAG/SanGIS regional GIS data from SANDAG's ArcGIS Enterprise portal (`geo.sandag.org`) via the built-in `arcgis` plugin (see `config.yaml`). It was adapted from the Worcester, MA fork, which targeted an ArcGIS Hub site.

## Data source: SANDAG/SanGIS ArcGIS Enterprise

Facts verified against the live services (keep these in mind when touching discovery or query code):

- **Services directory:** `https://geo.sandag.org/server/rest/services` — a standard ArcGIS Enterprise services directory. Directory-walking with `?f=json` works anonymously; the `Hosted/` folder is public (Library, Floodplain, parcels, etc.).
- **Auth-gated folders:** some folders (e.g. `Digital_Infrastructure`; `GeoDepot` was gated until mid-2026 and is now public) require sign-in. Discovery must skip them and handle 401/403/sign-in responses gracefully rather than failing the walk.
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

Copy `config-example.yaml` to `config.yaml`. Enable exactly one plugin. Config supports `${ENV_VAR}` substitution. For Lambda, `scripts/deploy.sh` ships `config.yaml` inside the package and the handler reads it from `$LAMBDA_TASK_ROOT`; the `OPENCONTEXT_CONFIG` env var is left empty because the full config (with the `instructions` block) exceeds Lambda's 4KB env-var cap. Top-level `server_name`, `server_version`, and `instructions` are surfaced in the MCP `initialize` response.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs ruff check + format (pinned to the `ruff==` version in `pyproject.toml`, which must match the rev in `.pre-commit-config.yaml`), pytest with an 80% coverage gate (inert plugins and the legacy Lambda entry point are omitted in `pyproject.toml`), pip-audit on `requirements.txt`, Go vet/test for `client/`, and `terraform fmt -check`, on push to main/develop and on PRs. No job holds AWS credentials, so `terraform validate` is deliberately not run.

## Conformance invariants (tested; keep them true)

- **Transport checks** run in `server/http_handler.py` before routing: a browser `Origin` outside the allowlist gets 403; an unsupported `MCP-Protocol-Version` header gets 400 with -32600 (deliberately not -32022); `initialize` is exempt. Both local entry points drive the same handler, so these are exercisable without a deploy.
- **Tool metadata**: every tool declares a top-level `title`, `annotations={"readOnlyHint": True, "openWorldHint": True}` (never `idempotentHint`), and a BINDING `outputSchema`.
- **Error classification**: anything the caller can cause raises `core.interfaces.ToolInputError` (logged at WARNING, no traceback). Exactly one plain `ValueError` remains in the plugin -- the Feature Service returning non-JSON, a genuine upstream fault that keeps its traceback -- and the shared validators raise none. A drift-guard test pins both counts; classify any new raise deliberately. Read numeric arguments through `_int_arg` / `_float_arg`, never bare `int()`/`float()` over `arguments`.
- **Structured output**: every successful tool result carries `structuredContent` in the shared envelope `{query, summary, caveats, <payload>}` where the payload key names its contents (`datasets`, `dataset`, `buckets`, `rows`, `fields`, `values`, `candidates`). Caveats carry a stable `code` from `CAVEAT_CODES` in `plugins/arcgis/plugin.py` and are generated from ONE `_Caveats` list so prose and structured array cannot drift; tests validate real output against the advertised schema and assert every caveat message appears verbatim in the text. Schemas are loose where the data is loose: `rows` are raw attributes, `total_matching` is null (not zero) when the count fails.
- **Timeout ladder**: `plugins.arcgis.timeout` (20s) < `aws.lambda_timeout` (28s) < API Gateway 29s. `config.yaml` wins over `prod.tfvars` for timeout/memory; `prod.tfvars` wins for `lambda_name`. CI asserts the ladder.
