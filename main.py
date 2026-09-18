"""Entrypoint for the fitness MCP server.

    python main.py                        # Streamable HTTP on settings.mcp_host:mcp_port
    python main.py --transport stdio      # for MCP clients that spawn a subprocess

Startup lives here rather than in server.py so that importing the app has no
side effects -- tests and MCP Inspector can import `server.mcp` without
creating a SQLite file or installing a tracer provider.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _configure_logging() -> None:
    """Keep MCP protocol traffic separate from application logs.

    stderr, never stdout: the stdio transport reserves stdout exclusively for
    JSON-RPC messages, and a stray log line there corrupts the stream.
    """
    from config import settings

    log_path = Path(settings.log_file)
    if not log_path.is_absolute():
        log_path = Path(__file__).resolve().parent / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s [%(levelname)s] [SERVER] %(message)s",
        handlers=[
            logging.FileHandler(log_path, mode="a", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the fitness MCP server")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"),
                        default="streamable-http")
    parser.add_argument("--host", default=None,
                        help="override the configured bind address")
    parser.add_argument("--port", type=int, default=None,
                        help="override the configured port")
    args = parser.parse_args(argv)

    from config import settings

    _configure_logging()
    from observability import configure_tracing

    configure_tracing(settings.otel_service_name)

    import database

    database.init_database()

    from server import mcp

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return 0

    import uvicorn

    from observability import wrap_asgi_app

    # Mirrors FastMCP.run_streamable_http_async(), but wraps the app so the
    # inbound traceparent is extracted and this server's tool spans join the
    # caller's trace instead of starting their own.
    uvicorn.run(
        wrap_asgi_app(mcp.streamable_http_app()),
        host=args.host or settings.mcp_host,
        port=args.port or settings.mcp_port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
