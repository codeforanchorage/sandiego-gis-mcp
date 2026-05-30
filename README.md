# OpenContext

<p align="center">
  <img src="docs/opencontext_logo.png" alt="OpenContext Logo" width="400">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

---

**Worcester GIS MCP** — a Worcester, MA fork of OpenContext. It serves the City of Worcester's open data portal ([opendata.worcesterma.gov](https://opendata.worcesterma.gov)), an ArcGIS Hub site, through the built-in `arcgis` plugin.

---

## Connect to the Worcester server

The server is live. Add it as a custom connector in Claude (same steps on Claude.ai and Claude Desktop):

1. **Settings → Connectors** (or **Customize → Connectors** on claude.ai)
2. **Add custom connector**
3. Name it e.g. `Worcester GIS` and paste the URL:

   ```
   https://worcester-gis.codeforanchorage.org/mcp
   ```

Quick health check from a terminal:

```bash
curl -sS -X POST https://worcester-gis.codeforanchorage.org/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
# → {"jsonrpc":"2.0","id":1,"result":{"status":"ok"}}
```

### Tools exposed

| Tool | Purpose |
| ---- | ------- |
| `arcgis__search_datasets` | Discover datasets by keyword (e.g. "parcels", "zoning") |
| `arcgis__get_dataset` | Fetch a dataset's metadata and service URL |
| `arcgis__query_data` | Query features from a dataset (supports `where`, `out_fields`, `limit`) |
| `arcgis__get_aggregations` | Facet counts across the catalog (e.g. by `type`, `tags`, `categories`) |

---

## Run locally

`config.yaml` is already committed for Worcester (the `arcgis` plugin pointed at `opendata.worcesterma.gov`), so no setup is needed to run the server locally:

```bash
pip install aiohttp pyyaml
python3 scripts/local_server.py      # serves http://localhost:8000/mcp
```

On startup it connects to the live portal and registers the four `arcgis__*` tools. Worcester's portal is public, so **no API token is required**. (On a Windows console you may need `PYTHONUTF8=1` for the startup banner's emoji.)

See [Getting Started](docs/GETTING_STARTED.md) for the generic OpenContext setup.

---

## Deploy & operate (Worcester)

Production runs on AWS Lambda + API Gateway behind `worcester-gis.codeforanchorage.org`, in `us-west-2`.

**First-time bootstrap** (state backend, once per account):

```bash
cd terraform/bootstrap
terraform init
terraform apply \
  -var="aws_region=us-west-2" \
  -var="state_bucket_name=worcester-gis-opencontext-tfstate" \
  -var="lock_table_name=terraform-state-lock"
```

These three values must match `terraform/aws/backend.tf`.

**Deploy / redeploy** (workspace defaults to `worcester-prod`):

```bash
./scripts/deploy.sh --environment prod
```

For a code-only change, that single command is all you need. The first stand-up of a new environment also creates an ACM certificate and an API Gateway custom domain — DNS is managed externally (no Route53), so on the first deploy you must:

1. **Validate the cert.** The first apply errors on `CreateDomainName` ("Certificate is not in an ISSUED state") — expected. Create the ACM validation CNAME (`terraform output acm_validation_cname_name`/`_value`, or read it from `aws acm describe-certificate`), wait for `ISSUED`, then re-run the deploy.
2. **Point the endpoint.** Create a CNAME `worcester-gis.codeforanchorage.org` → `terraform output -raw custom_domain_target`.

---

## Documentation


| Doc                                        | Description                                     |
| ------------------------------------------ | ----------------------------------------------- |
| [Getting Started](docs/GETTING_STARTED.md) | Setup and usage                                 |
| [Architecture](docs/ARCHITECTURE.md)       | System design and plugins                       |
| [Deployment](docs/DEPLOYMENT.md)           | AWS, Terraform, monitoring                      |
| [Testing](docs/TESTING.md)                 | Local testing (Terminal, Claude, MCP Inspector) |


---

## Examples

- **Boston OpenData (CKAN):** [examples/boston-opendata/config.yaml](examples/boston-opendata/config.yaml)
- **Custom plugin:** [examples/custom-plugin/](examples/custom-plugin/)

---

## Contributing

Pre-commit hooks (optional):

```bash
pip install pre-commit
pre-commit install
```

Hooks: Ruff, yamllint, gofmt, plus `detect-private-key` and `gitleaks` secret scanning. Run manually: `pre-commit run --all-files`.

> **Never commit secrets.** `config.yaml` is tracked, so any API token belongs in an environment variable referenced via `${ENV_VAR}` (e.g. `token: "${ARCGIS_TOKEN}"`), never inline. The gitleaks hook will block accidental commits of keys/tokens.

---

## License

MIT — see [LICENSE](LICENSE).

**Author:** Srihari Raman, City of Boston Department of Innovation and Technology

**Worcester fork** maintained for the City of Worcester, MA. OpenContext is MIT-licensed; this fork retains the original attribution above.
