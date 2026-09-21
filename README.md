# fitness-mcp

MCP tool server for the fitness agent. Exposes four tools over Streamable
HTTP: `search_food_nutrition`, `calculate_tdee_and_macros`, `log_entry`,
`get_daily_biometrics_summary`.

Consumed by [`fitness_agent`](../fitness_agent) through ADK's `McpToolset`.
This repository has no dependency on the agent, on ADK, or on any model SDK --
it answers tool calls and nothing else.

## Layout

| Path | What it is |
|---|---|
| `main.py` | Process entrypoint: logging, tracing, DB init, transport |
| `server.py` | FastMCP app and tool registrations (import-safe, no side effects) |
| `tools/database.py` | SQLite schema and CRUD for biometrics logs |
| `tools/openfoodfacts_client.py` | Adapter over the Open Food Facts SDK |
| `config.py` | Loads `server.yaml`; env vars override every value |
| `observability.py` | Tool spans and OpenTelemetry bootstrap |

## Running locally

```bash
uv sync
uv run python main.py
```

Serves Streamable HTTP on `http://127.0.0.1:8003/mcp`.

`search_food_nutrition` calls the public Open Food Facts API, which returns
503 under load often enough to matter; the tool reports the error rather than
inventing nutrition data.

## Storage

`log_entry` and `calculate_tdee_and_macros` persist to SQLite at
`settings.db_path` (default: `fitness_data.db` beside this module). On Cloud
Run that is the container's writable layer, so it is per-instance and lost on
restart. Point `FITNESS_DB_PATH` at a mounted volume if the log has to
survive a revision.

## Deployment

Deploys to Cloud Run as a private service; only the fitness agent's runtime
service account holds `roles/run.invoker` on it. The agent authenticates with
a Google-signed ID token whose audience is this service's URL.

Provisioning and deploy scripts live in
[`travel_agent/scripts`](../travel_agent/scripts).
