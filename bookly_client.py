"""
Talks to Bookly's live store API: MCP JSON-RPC first, REST tools.json if MCP is down.
tools.py calls execute_tool here after the auth ACL; this module does not know about Riley or AOPs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL = "https://bookly.davidbusacker.com/mcp"
DEFAULT_MANIFEST_URL = "https://bookly.davidbusacker.com/api/public/tools.json"
CACHED_MANIFEST_PATH = Path(__file__).resolve().parent / "data" / "bookly_tools.json"

_PATH_HINT = "Path segment"
_QUERY_HINT = "Query parameter"

# Bookly's MCP schema for these writes is wrong (append messages typed as
# strings; log requires per-message confidences). REST still accepts the
# original payload — use it so admin traces actually persist.
TRACE_WRITE_TOOLS = frozenset(
    {"log_agent_trace", "append_agent_trace_messages", "update_agent_trace"}
)


class BooklyClient:
    """Talk to Bookly OMS tools. Prefer MCP; fall back to REST."""

    def __init__(
        self,
        *,
        transport: str,
        tools: list[dict[str, Any]],
        instructions: str = "",
        mcp_url: str | None = None,
        manifest: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.transport = transport
        self.instructions = instructions
        self.mcp_url = (mcp_url or "").rstrip("/")
        self.manifest = manifest or {}
        self.timeout = timeout_seconds
        self._tools = tools
        self._tools_by_name: dict[str, dict[str, Any]] = {t["name"]: t for t in tools}
        self._rest: BooklyClient | None = None

    @classmethod
    def from_env(cls) -> "BooklyClient":
        prefer = os.environ.get("BOOKLY_TRANSPORT", "mcp").strip().lower()
        mcp_url = os.environ.get("BOOKLY_MCP_URL", DEFAULT_MCP_URL)
        if prefer != "rest":
            try:
                client = cls._from_mcp(mcp_url)
                logger.info("Bookly transport=mcp url=%s tools=%s", mcp_url, len(client._tools))
                return client
            except Exception:
                logger.exception("Bookly MCP unavailable; falling back to REST manifest")
        return cls._from_rest()

    @classmethod
    def _from_mcp(cls, mcp_url: str) -> "BooklyClient":
        init = _mcp_rpc(
            mcp_url,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "bookly-support-agent", "version": "1.1.0"},
            },
        )
        result = init.get("result") or {}
        listed = _mcp_rpc(mcp_url, "tools/list", {})
        tools = (listed.get("result") or {}).get("tools") or []
        if not tools:
            raise RuntimeError("MCP tools/list returned no tools")
        client = cls(
            transport="mcp",
            tools=tools,
            instructions=result.get("instructions") or "",
            mcp_url=mcp_url,
        )
        rest = cls._from_rest()
        client._rest = rest
        for name in TRACE_WRITE_TOOLS:
            rest_tool = rest._tools_by_name.get(name)
            if rest_tool and rest_tool.get("http"):
                client._tools_by_name[name] = {
                    **client._tools_by_name.get(name, {}),
                    "http": rest_tool["http"],
                    "inputSchema": rest_tool.get("inputSchema") or {},
                }
        return client

    @classmethod
    def _from_rest(cls) -> "BooklyClient":
        manifest_url = os.environ.get("BOOKLY_MANIFEST_URL", DEFAULT_MANIFEST_URL)
        manifest = _load_manifest(manifest_url)
        return cls(
            transport="rest",
            tools=manifest.get("tools") or [],
            instructions=manifest.get("instructions") or "",
            manifest=manifest,
        )

    def anthropic_tools(self) -> list[dict[str, Any]]:
        anthropic_tools: list[dict[str, Any]] = []
        for tool in self._tools:
            schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
            anthropic_tools.append(
                {
                    "name": tool["name"],
                    "description": _tool_description(tool),
                    "input_schema": schema,
                }
            )
        return anthropic_tools

    def execute_tool(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools_by_name and not (
            self._rest and name in self._rest._tools_by_name
        ):
            return {"ok": False, "error": f"Unknown tool '{name}'."}
        if name in TRACE_WRITE_TOOLS and self._rest is not None:
            return self._rest._execute_rest(name, tool_input or {})
        if self.transport == "mcp":
            return self._execute_mcp(name, tool_input or {})
        return self._execute_rest(name, tool_input or {})

    def ping(self) -> dict[str, Any]:
        return self.execute_tool("health", {})

    def _execute_mcp(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        try:
            envelope = _mcp_rpc(
                self.mcp_url,
                "tools/call",
                {"name": name, "arguments": tool_input},
            )
        except requests.RequestException as exc:
            return {"ok": False, "error": "bookly_request_failed", "detail": str(exc), "tool": name}

        if envelope.get("error"):
            return {"ok": False, "tool": name, "error": envelope["error"]}

        result = envelope.get("result") or {}
        payload = _content_payload(result)
        if result.get("isError"):
            return {"ok": False, "tool": name, "error": payload}
        if isinstance(payload, dict):
            return {"ok": True, "tool": name, **payload}
        return {"ok": True, "tool": name, "data": payload}

    def _execute_rest(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools_by_name[name]
        http = tool.get("http", {})
        method = http.get("method", "GET").upper()
        url_template = http.get("url", "")
        try:
            url, query, body = _build_request(url_template, tool.get("inputSchema", {}), tool_input)
            response = requests.request(
                method=method,
                url=url,
                params=query or None,
                json=body if method in {"POST", "PATCH", "PUT"} and body else None,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            return {"ok": False, "error": "bookly_request_failed", "detail": str(exc), "tool": name}
        return _normalize_response(name, response)


def tool_result_content(result: dict[str, Any]) -> str:
    return json.dumps(result, default=str)


def _mcp_rpc(url: str, method: str, params: dict[str, Any] | None, *, timeout: float = 30.0) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    response = requests.post(
        url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    parsed = _parse_mcp_response(response)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Unexpected MCP response for {method}")
    return parsed


def _parse_mcp_response(response: requests.Response) -> Any:
    ctype = (response.headers.get("Content-Type") or "").lower()
    text = response.text
    if "text/event-stream" in ctype or text.lstrip().startswith("event:"):
        data_lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if not data_lines:
            raise RuntimeError("MCP SSE response had no data lines")
        return json.loads(data_lines[-1])
    return response.json()


def _content_payload(result: dict[str, Any]) -> Any:
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            raw = block.get("text") or ""
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    return result


def _load_manifest(url: str) -> dict[str, Any]:
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        manifest = response.json()
        CACHED_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHED_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
        return manifest
    except (requests.RequestException, json.JSONDecodeError) as exc:
        if CACHED_MANIFEST_PATH.exists():
            return json.loads(CACHED_MANIFEST_PATH.read_text())
        raise RuntimeError(f"Could not load Bookly manifest from {url}: {exc}") from exc


def _tool_description(tool: dict[str, Any]) -> str:
    title = tool.get("title", "")
    desc = tool.get("description", "")
    annotations = tool.get("annotations", {})
    hints: list[str] = []
    if annotations.get("readOnlyHint"):
        hints.append("read-only")
    if annotations.get("destructiveHint"):
        hints.append("writes data")
    hint_text = f" ({', '.join(hints)})" if hints else ""
    if title and title not in desc:
        return f"{title}{hint_text}: {desc}"
    return f"{desc}{hint_text}"


def _build_request(
    url_template: str,
    input_schema: dict[str, Any],
    tool_input: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    properties = input_schema.get("properties", {})
    path_keys = _path_keys_from_url(url_template)
    path_values: dict[str, Any] = {}
    query: dict[str, Any] = {}
    body: dict[str, Any] = {}

    for key, value in tool_input.items():
        if value is None or value == "":
            continue
        prop = properties.get(key, {})
        description = prop.get("description", "")
        if key in path_keys or _PATH_HINT in description:
            path_values[key] = value
        elif _QUERY_HINT in description:
            query[key] = value
        else:
            body[key] = value

    url = url_template
    for key in path_keys:
        if key not in path_values:
            raise ValueError(f"Missing path parameter '{key}' for URL {url_template}")
        url = url.replace(f"{{{key}}}", str(path_values[key]))
        body.pop(key, None)
    return url, query, body


def _path_keys_from_url(url_template: str) -> list[str]:
    return re.findall(r"\{(\w+)\}", url_template)


def _normalize_response(tool_name: str, response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = {"raw": response.text[:2000]}

    if response.ok:
        return {"ok": True, "tool": tool_name, "status": response.status_code, **payload}

    if isinstance(payload, dict) and "error" in payload:
        return {
            "ok": False,
            "tool": tool_name,
            "status": response.status_code,
            "error": payload["error"],
        }
    return {
        "ok": False,
        "tool": tool_name,
        "status": response.status_code,
        "error": payload,
    }
