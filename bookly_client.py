"""
Bookly Support API client — manifest-driven tool execution.

Loads the MCP-style tool manifest from the Lovable Bookly OMS and executes
each tool as a real HTTP call. The agent never talks to Bookly directly;
Claude calls execute_tool(name, input) and this module maps that to REST.

Manifest: https://bookly-agent-api.lovable.app/api/public/tools.json
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

# Default manifest URL and on-disk cache (refreshed at startup if remote succeeds)
DEFAULT_MANIFEST_URL = "https://bookly-agent-api.lovable.app/api/public/tools.json"
CACHED_MANIFEST_PATH = Path(__file__).resolve().parent / "data" / "bookly_tools.json"

# How we classify each inputSchema property when building the HTTP request
_PATH_HINT = "Path segment"
_QUERY_HINT = "Query parameter"


class BooklyClient:
    """Execute Bookly public API tools defined in the MCP-style manifest."""

    def __init__(
        self,
        manifest: dict[str, Any],
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.manifest = manifest
        self.base_url = manifest.get("base_url", "").rstrip("/")
        self.instructions = manifest.get("instructions", "")
        self.timeout = timeout_seconds

        # name -> full tool definition (includes http + inputSchema)
        self._tools_by_name: dict[str, dict[str, Any]] = {
            tool["name"]: tool for tool in manifest.get("tools", [])
        }

    @classmethod
    def from_env(cls) -> "BooklyClient":
        """Load manifest from remote URL with local cache fallback."""
        manifest_url = os.environ.get("BOOKLY_MANIFEST_URL", DEFAULT_MANIFEST_URL)
        manifest = _load_manifest(manifest_url)
        return cls(manifest)

    def anthropic_tools(self) -> list[dict[str, Any]]:
        """
        Convert manifest tools to Anthropic Messages API tool definitions.
        Claude uses name, description, and input_schema to decide when/how to call.
        """
        anthropic_tools: list[dict[str, Any]] = []
        for tool in self.manifest.get("tools", []):
            anthropic_tools.append(
                {
                    "name": tool["name"],
                    "description": _tool_description(tool),
                    "input_schema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                }
            )
        return anthropic_tools

    def execute_tool(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Run one tool by name. Returns JSON-serializable result or error envelope."""
        tool = self._tools_by_name.get(name)
        if not tool:
            return {"ok": False, "error": f"Unknown tool '{name}'."}

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

    def ping(self) -> dict[str, Any]:
        """Quick connectivity check via the health tool."""
        return self.execute_tool("health", {})


def tool_result_content(result: dict[str, Any]) -> str:
    """Serialize tool output for Claude tool_result blocks."""
    return json.dumps(result, default=str)


def _load_manifest(url: str) -> dict[str, Any]:
    """Try remote manifest first; fall back to cached file if network fails."""
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        manifest = response.json()
        # Refresh local cache for offline dev
        CACHED_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHED_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
        return manifest
    except (requests.RequestException, json.JSONDecodeError) as exc:
        if CACHED_MANIFEST_PATH.exists():
            return json.loads(CACHED_MANIFEST_PATH.read_text())
        raise RuntimeError(f"Could not load Bookly manifest from {url}: {exc}") from exc


def _tool_description(tool: dict[str, Any]) -> str:
    """Combine title + description + read/write hint for Claude."""
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
    """
    Split tool_input into path substitutions, query params, and JSON body.

    Convention from Bookly manifest:
      - "Path segment" in property description -> URL {placeholder}
      - "Query parameter" in property description -> ?key=value
      - Everything else on POST/PATCH/PUT -> JSON body
    """
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

    # Ensure every URL placeholder has a value
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
    """Return a consistent envelope Claude can reason about."""
    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = {"raw": response.text[:2000]}

    if response.ok:
        return {"ok": True, "tool": tool_name, "status": response.status_code, **payload}

    # Bookly error shape: {"error": {"code", "detail", ...}}
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
