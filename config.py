"""Load server.yaml and expose it as `settings`.

Deliberately the same shape as travel_mcp/config.py. Host, port and the Open
Food Facts user agent used to be read straight from os.getenv at module import
in three different files; routing them through one declarative file means a
deploy-time override has one place to go and one documented default to read.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

DEFAULT_CONFIG_FILE = "server.yaml"
REPO_ROOT = Path(__file__).resolve().parent


def _parse(text: str, origin: str) -> dict[str, Any]:
    """Parse YAML or JSON by extension.

    safe_load is used deliberately -- plain load would construct arbitrary
    objects from config. JSON is still accepted so an override can be either.
    """
    if origin.endswith((".yaml", ".yml")):
        import yaml

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{origin} must contain a mapping at the top level")
    return data


def _load_from_gcs(uri: str) -> dict[str, Any]:
    """Read a config object from gs://bucket/object.

    Raises rather than falling back to defaults: an unreadable bucket would
    otherwise start the server on local defaults and fail later in a way that
    looks like a tool bug rather than a config problem.
    """
    from google.cloud import storage  # imported lazily; only needed for gs://

    bucket_name, _, blob_name = uri[len("gs://"):].partition("/")
    if not bucket_name or not blob_name:
        raise ValueError(f"malformed GCS config URI: {uri}")
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(blob_name)
    if not blob.exists():
        raise FileNotFoundError(f"config object not found: {uri}")
    return _parse(blob.download_as_text(), uri)


def load_config() -> dict[str, Any]:
    location = os.getenv("MCP_CONFIG_PATH", DEFAULT_CONFIG_FILE)
    if location.startswith("gs://"):
        return _load_from_gcs(location)
    path = Path(location)
    if not path.is_absolute():
        # Resolve against this file, not the cwd, so the server works no
        # matter which directory it is launched from.
        path = REPO_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    return _parse(path.read_text(encoding="utf-8"), path.name)


CONFIG = load_config()


def value(section: str, key: str, env_var: str, default: Any) -> Any:
    """Resolve a setting: environment variable, then config, then default."""
    section_data = CONFIG.get(section, {})
    configured = section_data.get(key) if isinstance(section_data, dict) else None
    return os.getenv(env_var, configured if configured is not None else default)


def _top(key: str, env_var: str, default: Any) -> Any:
    configured = CONFIG.get(key)
    return os.getenv(env_var, configured if configured is not None else default)


@dataclass(frozen=True)
class Settings:
    # Identity
    name: str = str(_top("name", "MCP_NAME", "health-biometrics-server"))

    # Bind address. Loopback by default so a local run is not exposed; the
    # container overrides FITNESS_MCP_HOST to 0.0.0.0 because Cloud Run routes
    # to the published port from outside the container's network namespace.
    mcp_host: str = str(value("server", "host", "FITNESS_MCP_HOST", "127.0.0.1"))
    mcp_port: int = int(value("server", "port", "FITNESS_MCP_PORT", 8003))

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "logs/agent.log")

    # SQLite lives on the container's writable layer, so on Cloud Run it is
    # per-instance and lost on restart. Configurable so a future deployment
    # can point it at a mounted volume without a code change.
    db_path: str = str(value("data", "db_path", "FITNESS_DB_PATH",
                             str(REPO_ROOT / "fitness_data.db")))
    off_user_agent: str = str(value("data", "off_user_agent", "OFF_USER_AGENT",
                                    "ADKHealthAgent/1.0 (contact@example.com)"))

    # Observability
    otel_enabled: bool = str(value("observability", "enabled", "OTEL_ENABLED",
                                   "true")).lower() in {"1", "true", "yes"}
    otel_service_name: str = str(value("observability", "service_name",
                                       "OTEL_SERVICE_NAME", "fitness-mcp"))


settings = Settings()
