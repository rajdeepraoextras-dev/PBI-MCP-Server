# Install

pbi-mcp is two stdio MCP servers, `pbi-model` and `pbi-report`. Pick one
install route below, then register both servers with your MCP host.

## Before you install

- **Save your report as a Power BI Project.** In Power BI Desktop enable, under
  *Options > Preview features*, the *Power BI Project (.pbip) save option*,
  *Store reports using enhanced metadata format (PBIR)* and *Store semantic
  model using TMDL format*, then use *Save as* and choose `.pbip`. The servers
  edit PBIR and TMDL folders; legacy `report.json` and `model.bim` projects
  are reported by `pbi_doctor` and not edited.
- **Close the project in Desktop while the assistant edits it**, and reopen it
  afterwards to see the changes.
- Power BI Desktop is only needed to open the result. The servers themselves
  never call it.

## Route 1: standalone bundle (no Python)

Download `pbi-mcp-standalone-win32-amd64.plugin` from the
[latest GitHub release](https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/releases/latest).
It is a self-contained Windows build: the Python runtime, dependencies and
vendored schemas are frozen inside one executable. Drag the file into a
plugin-aware host and both servers are registered for you.

To register the executable by hand, run it with the server name as its only
argument (`model` or `report`):

```json
{
  "mcpServers": {
    "pbi-model":  { "command": "C:\\tools\\pbi-mcp\\pbi-mcp.exe", "args": ["model"] },
    "pbi-report": { "command": "C:\\tools\\pbi-mcp\\pbi-mcp.exe", "args": ["report"] }
  }
}
```

The standalone build is about 20 MB and is published only as a release asset.
Rebuild it locally with `scripts/build_standalone.py`.

## Route 2: source plugin bundle

`pbi-mcp.plugin` (same release page) is the small source bundle. It needs
Python 3.11 or newer with the runtime dependencies on the host:

```bash
pip install "mcp<3" pydantic jsonschema
```

Drag it into a plugin-aware host. Its manifest launches
`python -m model_server.server` and `python -m report_server.server`.

## Route 3: `.mcpb` extension for Claude Desktop

If the release includes a `.mcpb` bundle, download it and double-click it, or
drag it onto Claude Desktop's *Settings > Extensions* page. Claude Desktop
installs and registers both servers. Availability of this bundle depends on
the release, so check the release assets.

## Route 4: `uvx` or `pip`

Run the console scripts straight from the repository with
[uv](https://docs.astral.sh/uv/), with nothing installed permanently:

```bash
uvx --from git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server pbi-model-server
uvx --from git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server pbi-report-server
```

or install them into a virtual environment:

```bash
pip install git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server
pbi-model-server     # stdio MCP server
pbi-report-server    # stdio MCP server
```

If the package is published on a package index, replace the Git URL with
`pbi-mcp`.

!!! warning "Check schema validation after a non-editable install"
    Report writes are validated against vendored Fabric schemas. Call
    `pbi_doctor()` once: its `schema_validation` finding says whether
    validation is active. If it reports the schemas are unavailable, use the
    source install below or a bundle.

## Route 5: from source

```bash
git clone https://github.com/rajdeepraoextras-dev/PBI-MCP-Server.git
cd PBI-MCP-Server
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"     # Windows; use .venv/bin/pip elsewhere
.venv\Scripts\python -m pytest            # a green suite means you are good to go
```

The servers run as modules from the repository root:

```bash
.venv\Scripts\python -m model_server.server
.venv\Scripts\python -m report_server.server
```

`scripts/package.py` regenerates `dist/claude_desktop_config.snippet.json`
with your absolute paths filled in.

## Register the servers with your host

Every host launches the servers over stdio, one entry per server. The examples
use `uvx`; substitute the executable and arguments from whichever route you
chose. In JSON, escape Windows backslashes (`\\`).

=== "Claude Desktop"

    Edit `claude_desktop_config.json` (on Windows,
    `%APPDATA%\Claude\claude_desktop_config.json`), merge in the block below
    and restart Claude Desktop.

    ```json
    {
      "mcpServers": {
        "pbi-model": {
          "command": "uvx",
          "args": ["--from", "git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server", "pbi-model-server"]
        },
        "pbi-report": {
          "command": "uvx",
          "args": ["--from", "git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server", "pbi-report-server"]
        }
      }
    }
    ```

    For a source checkout use its virtual environment instead:

    ```json
    {
      "mcpServers": {
        "pbi-model": {
          "command": "C:\\src\\PBI-MCP-Server\\.venv\\Scripts\\python.exe",
          "args": ["-m", "model_server.server"],
          "cwd": "C:\\src\\PBI-MCP-Server"
        },
        "pbi-report": {
          "command": "C:\\src\\PBI-MCP-Server\\.venv\\Scripts\\python.exe",
          "args": ["-m", "report_server.server"],
          "cwd": "C:\\src\\PBI-MCP-Server"
        }
      }
    }
    ```

=== "Claude Code"

    Add each server with the CLI. `--scope project` writes a shared
    `.mcp.json` next to your code; the default is your user profile.

    ```bash
    claude mcp add pbi-model  -- uvx --from git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server pbi-model-server
    claude mcp add pbi-report -- uvx --from git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server pbi-report-server
    claude mcp list
    ```

    The equivalent `.mcp.json`:

    ```json
    {
      "mcpServers": {
        "pbi-model":  { "command": "uvx", "args": ["--from", "git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server", "pbi-model-server"] },
        "pbi-report": { "command": "uvx", "args": ["--from", "git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server", "pbi-report-server"] }
      }
    }
    ```

=== "Cursor"

    Put this in `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one
    project), then enable the servers under *Settings > MCP*.

    ```json
    {
      "mcpServers": {
        "pbi-model": {
          "command": "uvx",
          "args": ["--from", "git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server", "pbi-model-server"]
        },
        "pbi-report": {
          "command": "uvx",
          "args": ["--from", "git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server", "pbi-report-server"]
        }
      }
    }
    ```

=== "VS Code"

    Create `.vscode/mcp.json` in the workspace (or run *MCP: Open User
    Configuration* for all workspaces). VS Code uses a top-level `servers`
    key and wants the transport spelled out.

    ```json
    {
      "servers": {
        "pbi-model": {
          "type": "stdio",
          "command": "uvx",
          "args": ["--from", "git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server", "pbi-model-server"]
        },
        "pbi-report": {
          "type": "stdio",
          "command": "uvx",
          "args": ["--from", "git+https://github.com/rajdeepraoextras-dev/PBI-MCP-Server", "pbi-report-server"]
        }
      }
    }
    ```

    Start the servers from the *MCP: List Servers* command, then use them in
    agent mode.

## Verify

Restart the host, then ask the assistant to call `pbi_doctor()` on each
server. It reports the Python and mcp versions, whether Power BI Desktop is
installed, and works before any project is selected. Next call
`pbi_set_project(path)` and follow the [Quickstart](quickstart.md).

Both mcp SDK majors are supported: `mcp>=1.2.0,<3` (the 1.x `FastMCP` and the
2.x `MCPServer`), selected automatically at import time.
