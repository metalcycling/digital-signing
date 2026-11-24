# Hetzner deployment

The production server was created on November 24, 2026. The deployment files below configure it; check the handoff status before assuming the public app is ready.

## Server and handoff status

- Hetzner server: `bello-agreements`, ID `177519650`, CX23 (2 vCPU, 4 GB RAM, 40 GB disk).
- Location: Falkenstein, Germany (`fsn1`). Helsinki capacity disappeared before creation; Germany had the same approved price.
- IPv4: `2.31.22.18`. Domain: `agreements.belloentertainment.com`.
- Approved monthly server + IPv4 total: EUR 5.99 at creation, with 0% VAT reported by the account. Paid Hetzner backups and extra volumes are not enabled.
- SSH: `ssh -o IdentitiesOnly=yes -i ~/.ssh/bello_hetzner_ed25519 root@2.31.22.18`. The private key stays outside this repository and Dropbox.
- Firewall: TCP 80/443 publicly accessible, TCP 22 restricted to the administrator's IP. If that IP changes, update the SSH source rule in the Hetzner console. Port 8000 is not public.
- Local deployment metadata and encrypted migration archive: `~/.config/bello-signing/`. Do not publish its contents or the API token.
- Ubuntu security updates, Python 3.12, uv, Caddy, the app, and private runtime data are installed. The two existing completed agreements and their PDFs were restored from the encrypted migration backup.
- Verification: 149 Python tests passed on Linux; 19 JavaScript tests passed locally. Synthetic PDF rendering, Drive access to 19 templates and both storage folders, and Gmail SMTP authentication passed. No test email was sent.
- A server backup was uploaded to Drive, downloaded, decrypted, and restored successfully. Daily backups are scheduled at 03:17 America/New_York, with log rotation and the existing 30-backup retention policy.
- **Live:** https://agreements.belloentertainment.com. Wix DNS resolves to the server, Caddy serves a valid HTTPS certificate and redirects HTTP, and both application services are enabled at boot. The public Google OAuth callback is registered; the owner and a colleague have confirmed successful staff sign-in, and the existing agreements are visible.
- Production Gmail delivery is enabled. SMTP authentication was verified without sending a test message. Both existing private completion links were checked over HTTPS, including rendered page images and final PDF bytes matching the stored originals. Anonymous staff API requests are rejected.

### Activation checklist (completed)

1. Add the A record above in Wix; leave the main website and mail records unchanged. Check authoritative DNS and public resolution. Do not add an AAAA record during this initial cutover.
2. In Google Cloud project `digital-signing-515013`, add `https://agreements.belloentertainment.com/auth/callback` to the existing Web OAuth client's authorized redirect URIs, retaining localhost. After confirming Google's saved settings, update the server's private `google-login-client.json` redirect list to match (or install the newly downloaded JSON, owner bello, mode 0600).
3. Run the production preflight as bello with `/etc/bello-signing.env` loaded. Then `systemctl enable --now bello-signing caddy`. Verify HTTPS, anonymous access restrictions, approved staff sign-in, and a private client link. Do not log OAuth or signing tokens.
4. Switch `SIGNING_EMAIL_MODE` from `local` to `gmail` in `/etc/bello-signing.env` after public-host checks and restart the app. Existing local outbox entries are not automatically sent. An end-to-end production test email has not been sent; use a new test agreement addressed to the owner when testing delivery.
5. Run another verified backup after final configuration. The optional Wix `/agreements` redirect has not been configured; use the public subdomain directly.

Do not resume using the Mac's agreement database for real changes after cutover; the server becomes the authoritative copy. The original Mac data is retained for recovery.

## Account and server

Create an account at https://console.hetzner.com/ and a project named **Bello Agreements**. Check current capacity and pricing before creating a server. Target a small x86_64 shared-CPU machine with 2–4 GB RAM and persistent local disk; use Ubuntu 24.04 LTS. The CX23 is a candidate only if actually available. Do not silently upgrade to a materially more expensive plan if unavailable.

Use SSH keys. A Hetzner Cloud firewall should allow SSH from the administrator's IP and HTTP/HTTPS (80/443) publicly. Do not expose port 8000. The existing Gmail sender uses outbound TCP 587. Retain outbound DNS and HTTPS for Google APIs, OAuth, certificate renewal, updates, and backups.

For assisted provisioning, a project-scoped Read & Write API token can be generated under Security → API Tokens. Store it in a private local file outside this repository/Dropbox, with owner-only permissions; never paste it into chat or commit it. Do not confuse the token with an SSH key.

## Files and dependencies

- Application source and uv.lock: `/opt/bello-signing`
- Runtime data and credentials: `/var/lib/bello-signing` (owner `bello:bello`, directory 0700, private files 0600)
- Production settings: `/etc/bello-signing.env` (root:bello, 0640)
- Service account: `bello`, without interactive login

Transfer only application source, pyproject.toml, uv.lock, and deployment files. Exclude `.env`, `client_secret.json`, `.venv`, caches, and local credentials from the source transfer. Do not copy the Mac virtualenv. Install Python 3.12, uv, Caddy using its official Ubuntu package instructions, cron, and logrotate. In the server checkout run `uv sync --frozen --no-dev --python /usr/bin/python3.12`. Keep the interpreter outside `/root` because the service protects home directories. Ensure the service user can read/execute the application and its virtualenv.

Copy `production.env.example` to `/etc/bello-signing.env` and fill actual folder IDs. Keep the file shell-compatible (quote values containing spaces) because the backup wrapper sources it. Start with `SIGNING_EMAIL_MODE=local` so staging tests cannot send real messages. The service loads this environment file; do not copy the development `.env` into the deployment.

## Data and Google setup

Before the final cutover, stop changing agreements on the Mac, run one last encrypted backup, download/transfer it and transfer `backup.key` separately over SSH. Restore into a new staging directory with the backup restore command. Validate the database and configuration, then move the verified data into `/var/lib/bello-signing` while the server app is stopped. Never run the Mac and production app concurrently against separate writable copies of the same agreement registry. Preserve backup-config.json and the same key to continue the existing backup namespace/retention.

Rebuild production settings from the example rather than blindly reusing runtime-settings.json. Install a production **Web application** OAuth client JSON containing `https://agreements.belloentertainment.com/auth/callback`; retain the approved staff list and existing company Drive token/Gmail credentials. If authorization scopes or consent-screen state require reauthorization, resolve that before cutover. Keep a separate secure copy of the backup key.

## DNS, HTTPS, and startup

In Wix DNS, add an A record for `agreements` pointing at the server's IPv4. Add an AAAA record only if IPv6 is configured and working. Leave the main website and mail records alone. Copy `Caddyfile` to `/etc/caddy/Caddyfile`, validate using `caddy validate --config /etc/caddy/Caddyfile`, and reload Caddy. Caddy manages HTTPS automatically once DNS and ports 80/443 work. See https://caddyserver.com/docs/install and https://caddyserver.com/docs/automatic-https.

Install `bello-signing.service` in `/etc/systemd/system/`, run `systemctl daemon-reload`, then enable/start it. Its preflight checks production URLs, auth, and credential permissions. Keep **one worker**, as current workflow locks are process-local. The service is bound to loopback and trusts proxy headers only from local Caddy. App access logging is disabled to avoid logging client signing tokens and OAuth callback codes. No Caddy access-log directive is enabled.

Verify authorized/unauthorized staff access, private client links without staff sessions, PDF rendering, corrections, and the completion/Drive archive flow using a disposable agreement. Check download and clipboard links use the public domain. Then switch email delivery to Gmail, restart, and explicitly test delivery to the owner's address. Local outbox messages are not automatically sent when Gmail is enabled.

Create Wix's `/agreements` redirect to `https://agreements.belloentertainment.com` after the public app works. Existing emails that contain localhost links still contain those old addresses; they are not rewritten by deployment.

## Backups and updates

Run `deploy/backup-production.sh` as bello and verify a downloaded backup restores successfully. Only then install `bello-backup.cron` into `/etc/cron.d/bello-backup` (root:root, 0644), enable the cron service, and install `backup-logrotate` into `/etc/logrotate.d/bello-backup`. The schedule is daily at 03:17 in the server timezone. Check backup-status.json and logs; no separate failure-alert service is configured.

Before an app update, take a verified backup, stop the app, deploy source and synchronize dependencies with the lockfile, then restart and test. Keep the previous release and database backup for rollback. Restore databases only with the app stopped; account for emails or Drive changes that happened after the chosen backup. Apply OS security updates and restart services when required.
