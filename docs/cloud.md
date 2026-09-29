# Cloud publishing (optional)

!!! warning "May be absent on your build"
    The `pbi-service` server described here is **optional**. It is not part of
    every build or bundle. If your host does not list a `pbi-service` server,
    you do not have it, and nothing else in this documentation depends on it.
    This page describes its purpose and safe usage in general terms; the tool
    list your host shows for the server is the authority on names and options.

## What it is for

`pbi-model` and `pbi-report` are strictly local: they read and write the
project files on disk and make no network calls. `pbi-service` is a separate,
optional server that covers the step after you are happy with the local result:

- **Publish** a local project (its report and semantic model) to a workspace in
  Microsoft Fabric / Power BI through the **Fabric REST API**.
- **Refresh** the published semantic model and check the refresh status.

Keeping this in its own server means the offline servers stay offline. You can
leave `pbi-service` unregistered and lose nothing else.

## Authentication

The server needs a Microsoft Entra ID (Azure AD) identity that is allowed to
write to the target workspace. Three ways to obtain one are typically offered;
use the narrowest that fits:

| Method | Best for | What you provide |
|---|---|---|
| **Access token** | a one-off publish | a short-lived bearer token you obtained elsewhere (for example from the Azure CLI). It expires, typically within an hour. |
| **Service principal** | unattended or CI runs | tenant ID, client (application) ID and a client secret or certificate for an app registration. |
| **Device code** | a person at a laptop | nothing up front: the server shows a URL and a short code, you sign in as yourself in a browser. |

For a service principal, the app registration must be added to the target
workspace with a role that can write items (Contributor or higher), and your
Fabric tenant admin must allow service principals to use Fabric APIs. A
personal or device-code sign-in needs the same workspace role for your account.

!!! danger "Keep secrets out of prompts and projects"
    Put client secrets and tokens in your host's secret store or environment
    variables, never in a chat message, a `.pbip` folder or a repository.

## A safe publishing flow

1. **Verify locally first.** `pbi_validate_project()`, `pbi_lint_page(page_id)`
   for each page, then open the `.pbip` in Power BI Desktop. Publishing a
   project Desktop cannot open only moves the problem to the cloud.
2. **Commit or copy the project.** Local `pbi_undo` history cannot revert
   something that has already been published.
3. **Publish to a development workspace first.** Publishing replaces the
   definition of the target item in the workspace, so never make your first
   attempt against a production workspace.
4. **Refresh, then check the status** before pointing anyone at the report.
5. **Promote** to production only after the development copy behaves.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 Unauthorized | the token expired, or was issued for a different audience; sign in again |
| 403 Forbidden | the identity has no write role on the workspace, or service principals are not allowed to use Fabric APIs in your tenant |
| 404 Not Found | wrong workspace ID, or the item was deleted or moved |
| 429 Too Many Requests | throttling; wait for the interval the service asks for and retry |
| Refresh fails right after publishing | the data source credentials on the published semantic model still need to be set in the service |

## Not covered

- Editing anything in the cloud. The project on disk stays the source of truth.
- Anything other than the operations the server lists. It does not run DAX or
  read report data.

Back to [Install](install.md), or see the [Safety model](safety.md) for the
local protections that apply before you publish.
