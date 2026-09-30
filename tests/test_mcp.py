"""His plug-in tools (MCP) for lookups: doctor's check of MCP_CONFIG and MCP_TOOLS. No network."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

import doctor

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def verdict(**settings) -> tuple[str, str]:
    doctor.results.clear()
    doctor.check_mcp(isolated(**settings))
    name, status, detail = doctor.results[-1]
    return status, detail


folder = Path(tempfile.mkdtemp())
config = folder / "mcp.json"
config.write_text(json.dumps({"mcpServers": {"ha": {"command": "ha-mcp", "env": {"HA_TOKEN": "x"}}}}))
os.chmod(config, 0o600)

check("nothing set: skipped", verdict()[0], "SKIP")
check("tools named, no file: said", verdict(mcp_tools="mcp__ha__get_state")[0], "WARN")
check("a file, its tools", verdict(mcp_config=str(config), mcp_tools="mcp__ha__get_state"),
      ("PASS", "1 tool(s) from 1 server(s) for her lookups"))
check("a tool from a server the file doesn't have", verdict(mcp_config=str(config), mcp_tools="mcp__spotify__now")[0], "FAIL")
check("a file with no tools named: nothing used", verdict(mcp_config=str(config))[0], "WARN")
check("a file that isn't JSON", verdict(mcp_config=str(folder / "nope.json"), mcp_tools="mcp__ha")[0], "FAIL")
os.chmod(config, 0o644)
check("readable by others: told to chmod 600", verdict(mcp_config=str(config), mcp_tools="mcp__ha")[0], "WARN")

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("mcp: doctor checks his plug-in tools' file and names")
