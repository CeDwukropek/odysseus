# Microsoft To Do → Samsung Reminder

This integration reads and writes Microsoft To Do directly. It does not copy tasks
into Odysseus Notes, its automation scheduler or the calendar. One Microsoft
connection and one selected list are stored per Odysseus owner. Phone delivery is
handled by Samsung/Microsoft synchronization and must be tested separately.

## Cost and prerequisites

Microsoft To Do is free with a personal Microsoft account. Standard Microsoft
Graph task CRUD is not a metered API. No paid Microsoft 365, Entra Premium or
per-task API purchase is needed for this personal-account integration.

App registration requires an existing Microsoft Entra directory where you have
permission to register apps. Creating a *new* workforce tenant has additional
Microsoft eligibility restrictions; do not assume that a fresh personal account
can create one without onboarding/subscription steps. If registration is blocked,
check existing directory access before purchasing anything. This integration does
not sign up for trials, enable billing or provision a tenant.

- [To Do personal accounts](https://support.microsoft.com/en-us/outlook/which-accounts-can-i-use-microsoft-to-do-with)
- [Standard API category](https://devblogs.microsoft.com/microsoft365dev/principles-for-monetizing-selected-microsoft-365-apis/)
- [App registration prerequisites](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)
- [Tenant creation restrictions](https://learn.microsoft.com/en-us/entra/fundamentals/create-new-tenant)

## 1. Register the application once

1. Open [Microsoft Entra](https://entra.microsoft.com/) and choose your existing
   directory. You need permission to register an app there.
2. App registrations → New registration. Name it e.g. **Odysseus To Do**.
3. Supported account types: **Accounts in any organizational directory and
   personal Microsoft accounts**. Personal Outlook/Live accounts must be included.
4. No redirect URI is needed for the device-code flow used here. Register.
5. Copy **Application (client) ID** from Overview. This ID is public; it is not a password.
6. Authentication → Advanced settings → **Allow public client flows → Yes** → Save.
7. API permissions → Add a permission → Microsoft Graph → Delegated permissions
   → **Tasks.ReadWrite**. The login also requests **offline_access** for renewal.
   Remove the automatically added User.Read permission if present and unused;
   this integration does not call the profile API. Organization policies may
   require an administrator to approve permission.
8. Do not create a Client Secret. Do not paste passwords or access tokens into chat.

## 2. Connect in Odysseus

1. Settings → Integrations → Add Integration → **Microsoft To Do / Samsung Reminder**.
2. Enter the Client ID and click **Connect Microsoft account**.
3. Open the displayed Microsoft sign-in link and enter the displayed code.
   Check the application name on Microsoft's consent screen.
4. Sign in to the Microsoft account you use on the phone and grant task access.
5. Back in Odysseus, choose the list that will sync with Samsung and click
   **Use selected list**. No list is silently selected for you.
6. **Read tasks** verifies list access without creating anything.

The sign-in state is temporary and bound to the logged-in owner. Closing the form
stops browser polling; after server restart begin sign-in again. Reconnecting
clears the selected list so a list from another Microsoft account cannot remain
active. Tokens are renewed automatically while permission remains valid.

## 3. Connect the phone

In Samsung Reminder: Settings → Sync with Microsoft To Do → sign in with the same
Microsoft account. In the To Do tab choose the **same list** as in Odysseus.
Samsung currently documents syncing one To Do list at a time. Optionally choose
Microsoft To Do under **Save reminders from other apps to** so Bixby/call-created
reminders also enter the synced list. Labels vary with device/language.

[Official Samsung/Microsoft sync instructions and limitations](https://support.microsoft.com/en-au/todo/sync-microsoft-to-do-with-the-samsung-reminder-app).

## 4. Ask the agent

Use explicit Microsoft/Samsung wording so it chooses this integration:

- „Pokaż przypomnienia z Samsung Reminder”.
- „Dodaj do Samsung Reminder: kup mleko; przypomnij jutro o 18:00”.
- „Oznacz to przypomnienie jako wykonane”.

Tool: `manage_microsoft_todo`. Actions: list_tasks, create_task, update_task,
complete_task, delete_task. The configured list and account come from the server;
the model cannot override them. Read IDs from list_tasks before changing/deleting.
Lists are paginated; follow next_cursor when returned. due_at is a deadline;
remind_at enables a timed alert. Datetimes require an explicit UTC offset, e.g.
2026-10-20T18:00:00+02:00. Null removes the corresponding date/reminder.

A successful result confirms Microsoft To Do only. Check the phone separately.
Do not retry a create blindly after a timeout: it may have succeeded remotely.
Read the list first to avoid duplicates. The first version does not implement
recurrence, attachments, checklist steps or Samsung-specific location alerts.
Existing local reminders and Google Calendar remain separate.

## Credentials and permissions

Data file: DATA_DIR/microsoft_todo.json, permissions 0600. Token bundles are
application-encrypted at rest, owner-scoped and never returned by status, setup
routes or agent tools. No tokens live in the preferences API or browser storage.
A separate lock file protects atomic updates. In-process per-owner locks serialize
refresh rotation and prevent a disconnect being undone by an in-flight login.
The temporary device-flow store expects the existing single-worker deployment.

Setup routes require an interactive owner session; bearer chat tokens cannot
manage OAuth connections. Agent execution follows the existing tool policy,
including private reads, cloud writes, untrusted task content and destructive
classification for deletion. Raw Microsoft error responses are not exposed.

**Disconnect** removes the local tokens, not Microsoft tasks. Revoke the app's
consent in your Microsoft account to revoke remote access as well.

## Tests and activation

Unit tests use a controlled Microsoft HTTP client: authorization, owner binding,
encrypted storage, expiry and refresh rotation, selected-list routing, validation,
CRUD, pagination, error redaction, native tool parsing and result formatting.
UI tests check list selection and text rendering of external task titles.

Real Microsoft login and Samsung phone delivery remain unverified until the user
provides their app registration Client ID and completes interactive consent.
No fake connection or cloud credentials are provisioned automatically.

API references: [device authorization](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code),
[task creation](https://learn.microsoft.com/en-us/graph/api/todotasklist-post-tasks?view=graph-rest-1.0),
[task resource](https://learn.microsoft.com/en-us/graph/api/resources/todotask?view=graph-rest-1.0).

## Deployment on an existing installation

The prepared image is `odysseus-todo:local`, layered on the calendar-fix image.
Only integration modules and their registration/UI files are replaced. On a dirty
installation, build these files from a three-way merge against the running files
so local features are retained; do not overwrite the original worktree.
`ops/microsoft-todo.compose.yml` selects this image for the existing odysseus
service. Preserve the existing Compose project, environment and data mounts;
recreate only odysseus with `--no-deps --no-build`. Original source files remain
unchanged. A standard rebuild from unpatched sources will remove the feature.

Rollback: recreate the service with the previous calendar-fix image/overlay.
The new token file is additive and ignored by the previous version. Disconnect
before removing the integration if credentials should be discarded. Do not
restore the entire database just to roll back this code.

Validation on 17 October 2026: 208 Python tests including integration, dispatcher,
permissions and external-content safeguards; 30 targeted tests against the actual
deployment image including calendar defaults; 2 UI tests and JS syntax checks.
These use controlled Microsoft responses. Real Microsoft consent and delivery to
Samsung remain pending the user's Client ID and phone setup.
