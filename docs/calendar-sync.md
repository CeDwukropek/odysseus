# Calendar defaults and Google CalDAV synchronization

## Behavior

Calendar Settings now has a per-user default calendar selector. The saved
`default_calendar_href` preference is used by both HTTP event creation and
`manage_calendar` whenever no calendar is specified. An explicit calendar takes
precedence. Unknown/ambiguous explicit references and unavailable saved defaults
produce an error instead of silently writing to another calendar. Setting the
default does not move or delete existing events. No database migration is needed.

`PUT /api/calendar/default-calendar` accepts `{"calendar_href":"owned-calendar-id"}`
or null to reset. `GET /api/calendar/calendars` returns `default_calendar_href`
and `is_default` per entry. Ownership is checked on selection and every write.

Writes return `sync.status`: `local`, `synced`, or `pending`. A failed remote
operation keeps the local write and retry marker, and the agent response/UI
explains that remote synchronization is pending. Do not retry creation as a new
event: use Sync to retry the original UID.

The UI now requests `/api/calendar/sync?direction=both`: pending writes/deletions
are pushed before pulling remote events. Counts and errors from both directions
are shown. The API default direction remains pull for backwards compatibility.
Write discovery also handles an empty principal response and maps Google principal
URLs to event collections, as pull already did. A lookup failure is treated as
an error; only NotFoundError means the event is absent, preventing accidental
recreation or false confirmation of remote deletion on network/auth failures.

## Verification

Calendar/CalDAV regression suite: 166 tests passed on 17 October 2026. This covers
persistent defaults, explicit selection precedence, account isolation, stale
selection, HTTP/agent writes, pending status, retries, Google discovery, timezone
handling, recurrence and client cleanup. The exact deployment image with local
calendar customizations preserved also passes targeted runtime regressions.

A real Google calendar roundtrip created, read, updated and deleted one temporary
event without attendees. This verified that the existing account configuration
can currently write; no OAuth migration was required for this installation.
Remote acceptance after deployment passed for the application tool path: creation
without a calendar argument selected the saved Google calendar, and updates and
deletion were verified remotely. Temporary events were removed.
No model inference is required to verify default selection.

## Deployment and rollback

Use a separate branch/worktree. On a locally modified installation, merge only
these modules into copies of the currently running files before building:

- routes/calendar_routes.py
- src/calendar_defaults.py (new)
- src/caldav_writeback.py
- src/tools/calendar.py
- static/js/calendar.js

Preserve existing customizations with a three-way merge and review conflicts.
Back up SQLite using its backup API, user preferences and running modules before
changing the service. Build a separate image `odysseus-calendar-fix:local` from
the current image with only the merged files replaced. The original image remains
available. The optional `ops/calendar-fix.compose.yml` overlay selects this image
for the odysseus service; retain the installation's existing Compose project,
environment and volumes, and use `--no-deps --no-build` to leave other services alone.
Do not publish private runtime files, credentials or backup data to Git.

To roll back, recreate only odysseus with the original Compose file/image without
the calendar-fix overlay. Preference keys are additive and ignored by the old
code. Restore a data backup only when necessary, since restoring it also undoes
new data written after the backup. A later rebuild from an unpatched source tree
will lose the fix; merge the reviewed branch when ready.
