"""Shared core library for the Options ORB MCP system.

Contains configuration, data models, persistence, option pricing, risk
management, the ORB strategy signal, spread construction, and the IBKR client
wrapper. The MCP servers under ``servers/`` are thin adapters over this core.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
