#!/usr/bin/env python3
"""
WeKnora MCP Server Package

A Model Context Protocol server that provides access to the WeKnora knowledge management API.
"""

__version__ = "0.2.0"
__author__ = "MWE Support"
__description__ = "Request-scoped multi-user MCP gateway for WeKnora v0.7.2"

from weknora_mcp_server import WeKnoraClient, run

__all__ = ["WeKnoraClient", "run"]
