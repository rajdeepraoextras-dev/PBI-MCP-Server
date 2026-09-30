# Security Policy

## Supported versions

Security fixes are made on `main` and released as a new patch version of the
latest minor release. Older releases are not patched; please upgrade.

| Version | Supported |
|---|---|
| Latest 2.x release | Yes |
| Anything older | No |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private reporting instead:
<https://github.com/rajdeepraoextras-dev/PBI-MCP-Server/security/advisories/new>
(repository *Security* tab, then *Report a vulnerability*).

Include what you found, the version (`pbi-mcp --version`), how you installed it,
and the smallest reproduction you can give: a tool call sequence or a synthetic
project. **Never attach a real `.pbip` project or credentials.**

This is a small open-source project maintained on a best-effort basis. Reports are
read as soon as possible, and you will be told whether the issue is accepted and
when a fix is expected. Please allow time for a fix before disclosing publicly;
reporters are credited in the release notes unless they prefer otherwise.

If private reporting is unavailable, open a public issue that says only that you
have a security concern and ask for a private channel. Do not include details.

## What the software does with your data

- **The `pbi-model` and `pbi-report` servers, and the `pbi-mcp` command, work on
  local files only.** They read and write the Power BI Project (`.pbip`) folders
  you point them at. They make no network connections, send no telemetry, and
  need no account, token or credential.
- **`pbi-service` is the only component that talks to a network,** and only when
  you explicitly run it (`pbi-mcp service`) and call its tools: it contacts
  Microsoft's Power BI / Fabric service on your behalf. It is a separate server
  and is never started by the model or report servers.
- **What your AI client sees.** Whatever a tool returns (table and column names,
  DAX, report structure, diffs) is delivered to your MCP host and therefore to
  whichever model provider that host uses. Do not point the tools at projects you
  are not allowed to share with that provider.
- **What gets written next to your project.** Atomic in-place edits;
  `*.bak-<timestamp>` backups of files before their first change; the undo
  journal under `<project>/.pbi-mcp/undo/`; and deleted visuals in
  `Report/.pbi/mcp-trash/`. None of these are read by Power BI Desktop, and none
  leave your machine. Treat them like the project itself when you share or commit
  it, because they contain earlier versions of your files.

## Running it safely

- **stdio (the default)** runs with your user's permissions. Tools can read and
  write anything you can, and some accept file paths (for example
  `pbi_add_image` and `pbi_project_diff`). Only connect servers to hosts and
  prompts you trust, and treat `.pbip` folders from unknown sources as untrusted
  input.
- **HTTP transports** (`pbi-mcp <server> --transport streamable-http|sse`) have
  **no authentication**. They bind to `127.0.0.1` by default. Binding elsewhere
  (`--host 0.0.0.0`) exposes tools that write to your disk to everyone who can
  reach the port, and the command prints a warning when you do. If you need
  remote access, put an authenticating reverse proxy or VPN in front, or use a
  private network.
- **Release integrity.** Releases are built by GitHub Actions from the tagged
  source. PyPI uploads use trusted publishing (short-lived OIDC credentials, no
  stored token). GitHub Release assets include `SHA256SUMS.txt`; verify
  downloads against it. The standalone executables are frozen from the same tag.

## Scope

In scope: arbitrary code execution or file writes outside what a tool is meant to
touch, path handling flaws, unsafe parsing of project files, unintended data
exfiltration, and weaknesses in the release and CI pipeline (for example
over-broad workflow permissions).

Out of scope: bugs in Power BI Desktop itself, vulnerabilities in the MCP host or
in your model provider, issues that need an already-compromised machine, and
denial of service by feeding an enormous project to a local tool.
