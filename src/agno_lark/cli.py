"""Command-line entry point for the Lark/Feishu AgentOS bridge."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import uvicorn

from .app import create_app
from .config import load_settings


def build_parser() -> argparse.ArgumentParser:
    """Create the ``agno-lark`` argument parser."""

    parser = argparse.ArgumentParser(
        prog="agno-lark",
        description="Run the Lark/Feishu bridge for an Agno AgentOS target.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind host (defaults to BRIDGE_HOST or 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (defaults to BRIDGE_PORT or 7788).",
    )
    parser.add_argument("--reload", action="store_true", help="Enable Uvicorn auto-reload.")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Load settings and run the FastAPI bridge with Uvicorn."""

    args = build_parser().parse_args(argv)
    settings = load_settings()
    host = str(
        args.host or getattr(settings, "bridge_host", None) or os.getenv("BRIDGE_HOST", "127.0.0.1")
    )
    configured_port = getattr(settings, "bridge_port", None)
    port = args.port or configured_port or int(os.getenv("BRIDGE_PORT", "7788"))
    app = create_app(settings)
    uvicorn.run(app, host=host, port=port, reload=args.reload)


__all__ = ["build_parser", "main"]
