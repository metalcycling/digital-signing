# Bello Agreements

A document-signing app built with Python, FastAPI, SQLite, and plain JavaScript. Google Docs supplies the templates, Gmail delivers notifications, and Google Drive stores completed PDFs and encrypted backups.

**Live app:** [agreements.belloentertainment.com](https://agreements.belloentertainment.com)

**Repository:** [metalcycling/digital-signing](https://github.com/metalcycling/digital-signing)

## Using the app

1. Sign in with an approved Google account, select a template, and create an agreement.
2. Edit the copied document in Google Docs, then render it in the app.
3. Review the pages, enter the client's details, customize the email and link expiration, and send.
4. The client fills the fields and signs through a private link, without a staff account.
5. The vendor receives an email alert, reviews the client's answers, and countersigns. Both parties receive the fully executed PDF; a copy is saved alongside the Google Docs agreement in Drive.

**Request corrections** sends the client a new link with their answers preserved; they must review and sign again. **Create revision** creates a new version for further changes. Older links are invalidated. **Copy link** copies the final Drive PDF link once an agreement is fully executed. Deletion requires confirmation and removes the agreement's active app and Drive records; older backups retain their contents until retention expires.

## Local development

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Node.js only for JavaScript tests. For a fresh checkout:

```sh
git clone git@github.com:metalcycling/digital-signing.git
cd digital-signing
uv sync --frozen
cp .env.example .env
```

Run an isolated demo with local email previews and no Google credentials:

```sh
SIGNING_MODE=demo SIGNING_AUTH_MODE=local SIGNING_EMAIL_MODE=local \
SIGNING_DATA_DIR="$HOME/.local/share/digital-signing-dev" \
uv run uvicorn digital_signing.app:app --reload --host 127.0.0.1 --port 8000
```

Open http://localhost:8000. Use the hosted app for real agreements: the Mac's old database is not synchronized with production. Local authentication bypass is restricted to loopback and must not be exposed publicly.

## Google connections

Keep downloaded credentials outside the repository. Configure `.env` for the environment being set up; see [.env.example](.env.example) for all settings. Production settings are loaded from `/etc/bello-signing.env`, as described in the [deployment guide](deploy/README.md).

| Connection | Setup |
| --- | --- |
| Google Drive | Enable the Drive API and create a **Desktop app** OAuth client. Set `SIGNING_MODE=google`, `SIGNING_TEMPLATE_FOLDER_ID`, and a separate `SIGNING_CONTRACTS_FOLDER_ID`. Run `uv run python -m digital_signing.connect_google /private/path/drive-client.json`. |
| Staff sign-in | Create a **Web application** OAuth client with `<SIGNING_APP_BASE_URL>/auth/callback` as an authorized redirect URI. Run `uv run python -m digital_signing.setup_auth --credentials /private/path/login-client.json`. Keep `SIGNING_AUTH_MODE=google` for production. |
| Staff access | Run `uv run python -m digital_signing.setup_auth --allow owner@example.com --allow colleague@example.com`. This **replaces the entire allowlist**; all approved staff can manage and countersign agreements. |
| Gmail | Run `uv run python -m digital_signing.setup_gmail` and enter the sender's Google app password at the hidden prompt. Set `SIGNING_EMAIL_MODE=gmail` to enable delivery. `local` stores email previews without sending. |

For localhost, use `http://localhost:8000/auth/callback`. Production uses `https://agreements.belloentertainment.com/auth/callback`, and both `SIGNING_APP_BASE_URL` and `SIGNING_EMAIL_BASE_URL` must point to the public HTTPS origin.

If a non-Workspace Google account displays an approval code, verify it with the intended person and run `uv run python -m digital_signing.setup_auth --approve-account CODE` in that environment. Staff login, the shared Drive connection, and Gmail credentials are independent. If Drive reports `invalid_grant`, reauthorize the Drive connection. External OAuth apps in Testing mode can require renewed authorization and configured test users.

## Template fields

Use the app's **Tag builder** to generate tags, then paste them into Google Docs. Keep each tag within one paragraph or table cell.

```text
{{client.provided_name|text|default=Jane Smith}}
{{client.provided_email|email|default=jane@example.com}}
{{client.name|text|label=Name of signer}}
{{client.phone|phone|default=555-0100}}
{{event.date|date}}
{{event.start|time}}
{{event.type|select|options=Wedding;Corporate|other=true}}
{{event.setting|select|options=Indoors;Outdoors|other=false}}
{{event.notes|textarea|optional}}
{{client.signature|signature}}
{{client.signed_on|signing_date}}
{{vendor.signature|signature}}
{{vendor.signed_on|signing_date}}
```

Supported types: `text`, `textarea`, `number`, `date`, `signing_date`, `time`, `phone`, `email`, `select`, `checkbox`, `initials`, and `signature`.

To avoid entering recipient details twice, set `default=` on `client.provided_name` and `client.provided_email` in the copied Google Doc before rendering. The sending dialog uses only those saved defaults and keeps them editable; it does not fall back to `client.name` or `client.email`. Leave `client.name` without a default so the client enters their name independently when signing. Missing tags/defaults, `REQUIRED` placeholders, and invalid email addresses leave the corresponding sending input empty. Temporary review answers do not change the saved defaults. Repeated tags must have identical definitions, including their defaults.

- Fields are required unless marked `optional`. Repeating the same named tag shares one answer; use different names for separate answers.
- `vendor.*` fields belong to the vendor by default. Use `role=client` or `role=vendor` to set ownership explicitly.
- `label=` sets the displayed label; `default=` supplies an editable, validated starting answer. Defaults cannot be used for signatures, initials, or signing dates.
- Dropdowns use semicolon-separated `options=`. Set `other=true` to permit custom text or `other=false` to restrict answers to the listed choices.
- Date defaults use `YYYY-MM-DD`; users see `MM-DD-YYYY` with the weekday. Time defaults use 24-hour `HH:MM` in 15-minute increments; the editor uses hours, minutes, and AM/PM. Signing dates are set automatically when each person signs.

Invitation bodies use the manually entered subject title in bold. Signing links default to seven days and their duration remains editable. Fully executed emails to the client and vendor include the signed PDF attachment without an app link or expiration notice. Final PDFs saved to Drive use the agreement name as the filename.

Use `locked=true` with a valid `default=` to prevent changes after rendering, for example `{{client.price|number|default=1000|locked=true}}`. Locked fields are enforced on submission. Signatures, initials, and automatic signing dates cannot be locked.

Calculated fields are automatically read-only, have no `default=`, and update from source answers:

```text
{{client.week_day|text|calc=weekday(client.event_date)}}
{{client.total_time|number|calc=duration_hours(client.start_time,client.end_time)}} hours
```

Sources must be ordinary date/time fields belonging to the same signer. Calculations stay blank until inputs are complete and are recalculated by the server on submission and after corrections. Duration uses clock hours in 15-minute increments: an earlier end time means the following day, with an explicit note; identical times are invalid. The tag builder includes both calculations. Missing sources, wrong types, and unsupported expressions block rendering.

Rendered input widths follow the field type, prefilled answer, and dropdown choices rather than the length of the tag. Longer answers remain accessible in the pop-up editor. Render a draft again to apply updated sizing; previously saved and signed versions keep their original layout.

## Tests

```sh
uv run pytest -q
node --test tests/*.cjs
```

Tests use temporary data and mocked external services; they do not send real emails or change Drive documents.

## Production and backups

The app runs on Hetzner behind Caddy HTTPS with **one Uvicorn worker**. Runtime data and credentials live at `/var/lib/bello-signing`; application code lives at `/opt/bello-signing`. See [deploy/README.md](deploy/README.md) for access, service management, deployment, and recovery procedures. Pushing to GitHub does not automatically deploy changes.

Encrypted backups run daily at **03:17 America/New_York**, retaining the latest **30** generations in Google Drive. They include SQLite records, rendered pages, signatures, PDFs, credentials, and settings. Source code is kept in GitHub; native Google Docs remain in Drive. Backups do not replace a separate secure copy of `backup.key`, which is never uploaded and is required for recovery.

On the server, run a manual backup with:

```sh
sudo -u bello /opt/bello-signing/deploy/backup-production.sh
```

Check `/var/lib/bello-signing/backup-status.json` and `backup-cron.log`; automatic failure emails are not configured. To restore a downloaded backup into a **new, nonexistent directory**:

```sh
uv run python -m digital_signing.backups restore /private/path/snapshot.backup \
  --key /private/path/backup.key --output /private/path/recovered-data
```

Restore checks encryption, file checksums, and database integrity. Stop the production app before replacing live data, retain the original data for rollback, and review settings before restarting. Do not commit `.env`, OAuth credentials, tokens, databases, signatures, or backup archives.
