"""Call one MCP tool (from the unified mcp_server.py) through stdio and write
the JSON envelope to a file (also printed, but the file is the reliable
channel -- see --output-file help).

Unified (2026-09-01, item 3) from three previously-separate, identical-except-
for-SERVER_PATH scripts (part1_mcp_call_tool.py / part2_mcp_call_tool.py /
part3_mcp_call_tool.py) into one, since they all now point at the same merged
server."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER_PATH = HERE / "mcp_server.py"


def content_to_json(result: Any) -> dict:
    content = getattr(result, "content", None)
    if not content:
        raise RuntimeError(f"No content in tool result: {result!r}")
    text = getattr(content[0], "text", None)
    if text is None:
        raise RuntimeError(f"First content item has no text: {content[0]!r}")
    return json.loads(text)


async def call_tool(tool_name: str, arguments: dict) -> dict:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return content_to_json(result)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tool_name")
    parser.add_argument("--arguments-json")
    parser.add_argument("--arguments-file")
    parser.add_argument(
        "--output-file",
        help=(
            "Write the JSON result here. Confirmed (2026-09-01, file-based trace "
            "instrumentation, item 1): this process always completes and flushes "
            "its stdout print successfully, but the parent's "
            "subprocess.run(capture_output=True) intermittently loses the piped "
            "stdout anyway when spawned from inside a busy nbconvert-launched "
            "Jupyter kernel (retries and PYTHONUNBUFFERED=1 did not help -- the "
            "child was never the problem). A plain file write has no such race, "
            "so the caller reads this file instead of proc.stdout."
        ),
    )
    args = parser.parse_args()

    if args.arguments_file:
        arguments = json.loads(Path(args.arguments_file).read_text(encoding="utf-8"))
    elif args.arguments_json:
        arguments = json.loads(args.arguments_json)
    else:
        raise SystemExit("Provide --arguments-json or --arguments-file.")

    payload = asyncio.run(call_tool(args.tool_name, arguments))
    payload["mode"] = "mcp_client"
    output = json.dumps(payload, ensure_ascii=False)
    if args.output_file:
        Path(args.output_file).write_text(output, encoding="utf-8")
    print(output)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
