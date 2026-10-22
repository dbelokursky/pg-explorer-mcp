"""JSON-RPC 2.0 and the MCP lifecycle for a server that only offers tools.

Covers protocol revisions 2024-11-05 to 2025-11-25: initialize, ping,
tools/list and tools/call. Revision 2026-07-28 drops the initialize handshake;
clients that speak it probe with server/discover, get "method not found" from
this server and fall back to initialize, as that revision prescribes.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from .tools import Tool, ToolError

log = logging.getLogger(__name__)

# Protocol revisions this server implements, newest first.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "Read-only access to one PostgreSQL database. Call status first: it shows the "
    "database, the role and the limits in force. Then use list_tables and "
    "describe_table to explore."
)


class InvalidParams(Exception):
    """The request's params are unusable; answered with JSON-RPC error -32602."""


def negotiate_protocol_version(requested: Any) -> str:
    """Choose the protocol revision to put in the initialize reply.

    `requested` is the raw params.protocolVersion from the client. It comes over
    the wire, so it can be a revision listed in SUPPORTED_PROTOCOL_VERSIONS, one
    this server has never heard of (newer or older), or not a string at all.

    MCP lifecycle rules: if the server supports the requested revision it MUST
    answer with the same one; otherwise it MUST answer with another revision it
    supports, which SHOULD be its latest. The client then decides whether it can
    work with that answer or disconnects.
    """
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    # Unknown, missing or malformed: offer our latest and let the client decide.
    # Rejecting would lock out a sloppy client that may well speak our revision.
    return SUPPORTED_PROTOCOL_VERSIONS[0]


class Server:
    """Turns one incoming JSON-RPC line into at most one reply line."""

    def __init__(self, tools: List[Tool], name: str = "pg-mcp", version: str = __version__) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._order = [tool.name for tool in tools]
        self._info = {"name": name, "version": version}
        self.protocol_version: Optional[str] = None
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
            "initialize": self._initialize,
            "ping": lambda _params: {},
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }

    def handle_line(self, line: bytes) -> Optional[bytes]:
        try:
            message = json.loads(line)
        except ValueError:
            return _encode(_error(None, PARSE_ERROR, "Parse error: the line is not valid JSON"))
        if isinstance(message, list):
            # Batches exist only in revision 2025-03-26; answering them costs nothing.
            replies = [reply for reply in map(self.handle, message) if reply is not None]
            return _encode(replies) if replies else None
        reply = self.handle(message)
        return None if reply is None else _encode(reply)

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "Invalid request: expected a JSON object")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            if "result" in message or "error" in message:
                return None  # a response, but this server never sends requests
            return _error(message.get("id"), INVALID_REQUEST, "Invalid request: expected JSON-RPC 2.0 with a method")
        if "id" not in message:
            return None  # notifications (initialized, cancelled, ...) need no reply
        msg_id = message["id"]
        handler = self._handlers.get(method)
        if handler is None:
            return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        try:
            result = handler(params)
        except InvalidParams as exc:
            return _error(msg_id, INVALID_PARAMS, str(exc))
        except Exception:
            log.exception("%s failed", method)
            return _error(msg_id, INTERNAL_ERROR, f"Internal error in {method}; details are in the server log")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        self.protocol_version = negotiate_protocol_version(requested)
        client = params.get("clientInfo")
        client_name = client.get("name") if isinstance(client, dict) else None
        log.info("initialize from %s: asked for %s, answering %s", client_name or "unknown client", requested, self.protocol_version)
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": self._info,
            "instructions": INSTRUCTIONS,
        }

    def _tools_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"tools": [self._tools[name].describe() for name in self._order]}

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise InvalidParams(f"Unknown tool: {name!r}. Available tools: {', '.join(self._order)}")
        try:
            text, is_error = tool.call(params.get("arguments")), False
        except ToolError as exc:
            text, is_error = str(exc), True
        return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _encode(payload: Any) -> bytes:
    # ensure_ascii escapes everything beyond ASCII and json never emits raw
    # newlines, so one reply is always exactly one ASCII line.
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
