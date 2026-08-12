# Design: Power BI Dataset Attachments

## Summary

Let users attach a Power BI dataset to a chat the same way they attach a knowledge
base today: browse the workspaces they have access to, pick a dataset by name, and
attach it. Once attached, the dataset id (and workspace id) is injected into the
model's context so that a Power BI MCP server — already configured as a tool server
in Open WebUI — executes its tools (DAX queries, schema inspection, etc.) against
the right dataset without the user ever pasting a GUID.

This reuses three existing mechanisms rather than inventing new ones:

1. **Attachment UI** — the `+` input menu / `#` command panels, modeled on the
   existing Knowledge drill-down (`src/lib/components/chat/MessageInput/InputMenu/Knowledge.svelte`).
2. **The `files` payload** — attachments already travel as a heterogeneous,
   `type`-discriminated list to the backend and land in `metadata['files']`
   (`backend/open_webui/utils/middleware.py:2703-2717`).
3. **Context injection** — the same pattern used for `<attached_knowledge>` tags
   appended to the system message (`middleware.py:2867-2886`).

## User experience

1. User clicks `+` in the message input (or types `#`) and picks a new
   **Power BI** tab.
2. The panel lists the workspaces the *user* can access (fetched with their own
   AAD token — not a service principal), with debounced search.
3. Expanding a workspace lists its datasets by name (same chevron drill-down as
   Knowledge → files).
4. Selecting a dataset attaches a chip to the input
   (`FileItem` chip labeled "Power BI Dataset").
5. On send, the model's context contains an `<attached_powerbi_datasets>` block
   with the dataset/workspace ids and names, and (recommended hardening, see
   below) MCP tool calls get the dataset id bound server-side.
6. The chip persists at the chat level (like documents do), so follow-up turns
   keep the dataset in context.

Attaching a dataset does **not** by itself enable the MCP server — the user (or a
deep link / model default) still selects the Power BI tool server via the
integrations menu (`selectedToolIds`). The panel should nudge: if the Power BI MCP
server is configured but not toggled on, auto-enable it on attach (frontend can
push `server:mcp:<id>` into `selectedToolIds`).

## Architecture

```
┌──────────────┐   GET /api/v1/powerbi/workspaces          ┌─────────────────┐
│ InputMenu/    │──────────────────────────────────────────▶│ routers/         │
│ PowerBI.svelte│   GET /api/v1/powerbi/workspaces/{id}/    │ powerbi.py       │──▶ Power BI REST API
└──────┬───────┘        datasets                            │ (user AAD token) │    api.powerbi.com
       │ onSelect({type:'powerbi_dataset', ...})            └─────────────────┘
       ▼
files=[..., {type:'powerbi_dataset', id, name, workspace_id, workspace_name}]
       │  chat completion request (files: [...])
       ▼
process_chat_payload (middleware.py)
  ├─ metadata['files'] ← files  (existing, middleware.py:2703-2717)
  ├─ NEW: collect powerbi_dataset items → metadata['powerbi_datasets']
  ├─ NEW: append <attached_powerbi_datasets> block to system message
  └─ existing MCP branch connects Power BI MCP server (middleware.py:2737-2789)
       │
       ▼
Model calls MCP tool → execute_tool_call (middleware.py:4969)
  └─ NEW (hardening): bind dataset_id/workspace_id into tool args server-side
```

## Backend

### 1. New router: `backend/open_webui/routers/powerbi.py`

A thin authenticated proxy over the Power BI REST API, modeled on the external
knowledge routes (`routers/knowledge.py:632-1044`) and the tool-server verify
probe (`routers/configs.py:545`) for outbound `aiohttp` conventions
(`trust_env=True`, `AIOHTTP_CLIENT_TIMEOUT`, `AIOHTTP_CLIENT_SESSION_SSL`).

Routes (all `Depends(get_verified_user)`):

| Route | Power BI API | Notes |
|---|---|---|
| `GET /api/v1/powerbi/workspaces` | `GET https://api.powerbi.com/v1.0/myorg/groups` | Supports `?query=` (server-side `$filter=contains(name,...)`) and `$top`/`$skip` paging to match the panel's infinite scroll. |
| `GET /api/v1/powerbi/workspaces/{workspace_id}/datasets` | `GET .../groups/{id}/datasets` | Returns `{id, name, configuredBy, isRefreshable}` per dataset. |
| `GET /api/v1/powerbi/status` | — | Reports whether Power BI auth is available for this user (drives the "Connect" state in the panel). |

Register in `main.py` alongside the other routers (`main.py:785-829`) with prefix
`/api/v1/powerbi`, gated on a new `ENABLE_POWERBI_INTEGRATION` PersistentConfig
(default off), following the existing config-key conventions in
`backend/open_webui/config.py`.

Because the calls use the **user's** token, "which workspaces they have access to"
is answered by Power BI itself — no access-control mirroring on our side.

### 2. Auth: getting a per-user Power BI token

Power BI's REST API needs an AAD token with scope
`https://analysis.windows.net/powerbi/api/.default` (delegated:
`Workspace.Read.All`, `Dataset.Read.All`). Two supported modes, in order of
preference:

**Mode A — dedicated OAuth client via `OAuthClientManager` (recommended).**
The MCP OAuth machinery (`backend/open_webui/utils/oauth.py:815`) is
provider-agnostic: clients are registered under arbitrary string ids
(`mcp:<server_id>` today), it supports static client credentials
(`oauth.py:633`), custom scopes (`apply_connection_oauth_options`,
`oauth.py:741`), and per-user sessions with refresh (`OAuthSessions`). Register a
client under id `powerbi` at startup (mirroring `main.py:579-596`) from admin
config (`powerbi.client_id`, `powerbi.client_secret`, `powerbi.tenant_id`,
authority `https://login.microsoftonline.com/{tenant}`). The existing
authorize/callback routes (`main.py:2589` / `main.py:2632`) then work as-is; the
router fetches tokens with
`oauth_client_manager.get_oauth_token(user.id, 'powerbi')`.

The frontend already has the redirect dance for exactly this
(`initiateOAuthRedirect`, `src/lib/apis/configs/index.ts:568`); the Power BI
panel shows a "Connect Power BI" button when `/status` reports no session,
mirroring the `authenticated: false` handling in
`IntegrationsMenu.svelte:398-407`.

**Mode B — reuse the Microsoft SSO token.** If the deployment logs users in with
Microsoft SSO (`config.py:2641-2656`) *and* the login scopes were extended to
include the Power BI scope, the router can use
`get_system_oauth_token(request, user)` (`middleware.py:3158-3190`). Cheaper to
deploy but couples login config to Power BI; offer it as a config toggle
(`powerbi.auth_mode: oauth_client | sso`). Note the SSO token only works if the
admin added the Power BI scope to `MICROSOFT_OAUTH_SCOPE`; `/status` should
verify with a cheap probe call rather than assume.

The same choice governs how the **MCP server** authenticates — see
"MCP server auth" below.

### 3. Payload interception in `process_chat_payload`

In `backend/open_webui/utils/middleware.py`, immediately after files are deduped
and moved into metadata (`middleware.py:2703-2717`):

```python
powerbi_datasets = [f for f in (files or []) if f.get("type") == "powerbi_dataset"]
if powerbi_datasets:
    metadata["powerbi_datasets"] = powerbi_datasets
```

Leave the items in `metadata['files']` — confirmed safe: `get_sources_from_items`
(`backend/open_webui/retrieval/utils.py:1324`) silently skips unknown types, so
the RAG pipeline ignores them. But `chat_completion_files_handler` decides
whether to run query generation based on the files list (`middleware.py:1833`),
so it should also skip pure-Power-BI attachments: if *every* item in
`metadata['files']` is a `powerbi_dataset`, skip the handler entirely (no wasted
query-generation LLM call, no empty retrieval pass).

### 4. Context injection

Mirror the `<attached_knowledge>` mechanism (`middleware.py:2867-2886`): when
`metadata['powerbi_datasets']` is non-empty, append to the system message via
`add_or_update_system_message(..., append=True)`:

```xml
<attached_powerbi_datasets>
  <dataset id="cfafbeb1-8037-4d0c-896e-a46fb27ff229"
           name="Sales Analytics"
           workspace_id="f089354e-8366-4e18-aea3-4cb4a3a50b48"
           workspace_name="Finance" />
</attached_powerbi_datasets>
<powerbi_instructions>
The user has attached the Power BI dataset(s) above to this conversation.
When answering questions about this data, use the Power BI tools and pass the
dataset id (and workspace id where required) from the attached dataset. Do not
ask the user for a dataset id.
</powerbi_instructions>
```

This runs in the same block that injects attached-knowledge manifests, and unlike
that block it should **not** be gated on native function calling — the dataset
manifest is useful in legacy tool-calling mode too. It must run *before* the
pre-RAG snapshot of the system prompt if we want it re-applied correctly across
native tool-call rounds; the existing loop restores `metadata['system_prompt']`
and re-applies context each round (`middleware.py:5149-5199`), so injecting into
the system message *before* the snapshot at `middleware.py:2948` makes the
manifest part of the restored baseline for free.

### 5. Hardening: bind the dataset id into MCP tool calls (recommended)

Prompt injection alone works, but models occasionally hallucinate GUIDs, and a
user could be socially engineered into a chat that queries someone else's
dataset id. Two complementary server-side bindings:

**5a. Argument binding at execution.** In `execute_tool_call`
(`middleware.py:4969-5007`), when the tool is from the Power BI MCP server and
exactly one dataset is attached, overwrite/insert the id parameters before
dispatch:

```python
if metadata.get("powerbi_datasets") and tool.get("server_id") == powerbi_server_id:
    ds = metadata["powerbi_datasets"]
    if len(ds) == 1:
        for key in ("dataset_id", "datasetId"):
            if key in tool["spec"]["parameters"]["properties"]:
                tool_function_params[key] = ds[0]["id"]
        # same for workspace_id / groupId
```

Note `execute_tool_call` already filters params to the spec's properties
(`middleware.py:4980-4981`), so this composes cleanly. With multiple attached
datasets, skip binding and let the model choose from the manifest — but
**validate** that the id it chose is one of the attached ids, and return a tool
error otherwise. Identifying "the Power BI MCP server" is config: an admin marks
the tool-server connection (a `powerbi: true` flag in the connection's `config`
dict, editable in `AddToolServerModal.svelte`), or we match on a configured
server id.

**5b. Header binding (alternative for header-aware MCP servers).** If the MCP
server accepts the dataset id as a request header, add a
`{{POWERBI_DATASET_IDS}}` template variable in
`backend/open_webui/utils/headers.py:115-131`, populated from
`metadata['powerbi_datasets']`, and let admins reference it in the connection's
custom headers (threaded through `build_tool_server_headers`,
`utils/tools.py:171-174`, at `connect_mcp_server`, `middleware.py:2222`).
Caveat: the MCP client connects once per request *before* tool calls
(`middleware.py:2737-2789`), so headers are per-conversation-turn, which is fine
for this use case. This is optional; 5a works with any MCP server whose tools
take a dataset id parameter.

### MCP server auth

The Power BI MCP server itself needs to call Power BI as the end user. Nothing
new is required: configure the connection with `auth_type: system_oauth` (which
forwards the user's SSO token, `utils/tools.py:153-157` — Mode B) or
`oauth_2.1`/`oauth_2.1_static` for a dedicated per-user grant handled by the MCP
server's own AS (Mode A). This design only guarantees the *dataset selection*
reaches the server; token plumbing is the existing tool-server config surface.

## Frontend

### 1. Panel: `src/lib/components/chat/MessageInput/InputMenu/PowerBI.svelte`

Copy the structure of `InputMenu/Knowledge.svelte`:

- Top level lists **workspaces** via `getItemsPage`-style paged fetch
  (`Knowledge.svelte:141-173` pattern) against the new
  `src/lib/apis/powerbi/index.ts` client, with `SearchInput` + infinite scroll.
- Chevron expands a workspace and lists its **datasets**
  (`Knowledge.svelte:56-94` drill-down pattern).
- Selecting a dataset emits:

```js
onSelect({
  type: 'powerbi_dataset',
  id: dataset.id,
  name: dataset.name,
  workspace_id: workspace.id,
  workspace_name: workspace.name,
  status: 'processed'
});
```

- If `/api/v1/powerbi/status` says the user isn't connected, render a
  "Connect Power BI" button → `initiateOAuthRedirect('powerbi')`
  (`src/lib/apis/configs/index.ts:568` pattern; the post-redirect restore hook in
  `Chat.svelte:1869-1876` already reopens state).
- On attach, if the configured Power BI MCP tool server isn't in
  `selectedToolIds`, add `server:mcp:<id>` so the tools are live for the send.

Wire the tab into `InputMenu.svelte` next to Knowledge/Notes/Files/Chats
(`InputMenu.svelte:507-564`), visible only when the integration is enabled
(config flag exposed via the existing frontend config endpoint). Optionally add a
`#` command panel via `CommandSuggestionList.svelte:119` later; the `+` menu is
the MVP.

### 2. Required allowlist changes in `Chat.svelte` (easy to miss)

Attachments are filtered by hardcoded type lists **twice**; without these edits
the chip renders but the item never reaches the server:

- `submitPrompt` filter — `Chat.svelte:2496-2502`: add `'powerbi_dataset'` to
  `['doc','text','note','chat','folder','collection']`. This also gives us
  chat-level persistence via `chatFiles` (the dataset stays attached for
  follow-up messages, like documents do).
- Request assembly filter — `Chat.svelte:3004-3010`: same addition.

### 3. Chip rendering

`src/lib/components/common/FileItem.svelte` falls through to a generic document
icon and capitalizes unknown types (`FileItem.svelte:104-158`), which would show
"Powerbi_dataset". Add a branch: Power BI icon (bundled SVG asset), label
"Power BI Dataset", secondary line = workspace name. No modal on click (or a
minimal one showing ids for debugging).

## Access control & security

- **Browsing** uses the user's own token, so workspace/dataset visibility is
  enforced by Power BI. The backend proxy must never fall back to any shared or
  admin credential for list calls.
- **Attachment validation**: ids arrive from the client, so at chat time the
  backend should treat them as claims, not facts. The hardening in §5a plus the
  MCP server authenticating as the end user means a forged dataset id fails at
  the Power BI API with a 403 — the design intentionally keeps Power BI as the
  authority rather than caching entitlements in Open WebUI.
- **Tool-server access**: whether a user may use the Power BI MCP server at all
  stays governed by the existing connection access grants
  (`has_connection_access`, `backend/open_webui/utils/access_control/__init__.py:148`).
- **Token storage**: OAuth sessions are already encrypted at rest by the
  `OAuthSessions` machinery; no new storage is introduced.
- **Temporary chats**: file items in temp chats are extracted client-side and
  never uploaded; `powerbi_dataset` items carry no content, so they can pass
  through unchanged — verify the temp-chat branch in
  `MessageInput.svelte:836-855` doesn't try to read them as files.

## Configuration surface

| Key | Default | Purpose |
|---|---|---|
| `powerbi.enable` | `false` | Master switch; hides the panel and router. |
| `powerbi.auth_mode` | `oauth_client` | `oauth_client` (Mode A) or `sso` (Mode B). |
| `powerbi.client_id` / `client_secret` / `tenant_id` | — | AAD app registration for Mode A (secret encrypted like other config secrets). |
| `powerbi.mcp_server_id` | — | Which tool-server connection is "the" Power BI MCP server (drives auto-enable + arg binding). Alternatively a `powerbi: true` flag on the connection config. |

Admin UI: a small section under Settings → Tools or a dedicated
Settings → Integrations panel; MVP can be env/config-file only.

## Implementation plan

1. **Backend router + auth** — `routers/powerbi.py`, OAuth client registration,
   config keys, `/status` endpoint. Testable standalone with curl.
2. **Frontend panel + attach flow** — API client, `PowerBI.svelte`, `InputMenu`
   tab, `Chat.svelte` allowlists, `FileItem` branch.
3. **Context injection** — middleware interception, `<attached_powerbi_datasets>`
   system-message block, skip-RAG-handler tweak.
4. **Hardening** — MCP arg binding in `execute_tool_call`, attached-id
   validation, auto-enable of the MCP tool server on attach.
5. **Polish** — `#` command panel, admin settings UI, i18n strings for the new
   labels (`src/lib/i18n`), docs.

Steps 1–3 are a functional MVP (model gets the ids via prompt); step 4 is what
makes it robust.

## Open questions

1. **Multiple datasets per chat** — allowed by this design (manifest lists all;
   arg binding only when exactly one). Is that the desired UX, or should
   attaching a second dataset replace the first?
2. **Semantic-model metadata in context** — should we also fetch table/column
   names at attach time and include a schema summary in the manifest? Improves
   first-shot DAX accuracy but adds latency and token cost; the MCP server
   likely exposes a schema tool already, so MVP says no.
3. **`My workspace`** — the personal workspace isn't in `/groups`; include it via
   `GET /v1.0/myorg/datasets` as a synthetic top-level entry?
4. **Reports/dashboards later** — the `type` discriminator
   (`powerbi_dataset`) leaves room for `powerbi_report` etc.; the panel drill-down
   would grow a type filter.
