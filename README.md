# speld_meet

In-bench Jitsi Meet for the De Speld Frappe stack. Brings up Prosody,
Jicofo, JVB and Coturn as Procfile-supervised processes alongside the
bench's existing workers; owns a `Meeting` DocType that frappe_appointment
booking pages and any future calendar app can target via stable
`/meet/<room-slug>` URLs.

Companion app to [`leme-leme/speld`](https://github.com/leme-leme/speld).
No fork of upstream Jitsi — the binaries are downloaded at install time
into `apps/speld_meet/vendor/`, the configs are rendered from Jinja
templates using secrets stored in `site_config.json`.

Repo: `leme-leme/speld_meet`. AGPL-3.0.

---

## Install

```bash
bench get-app https://github.com/leme-leme/speld_meet
bench --site <site> install-app speld_meet

# Then bring Jitsi up. Must be run on the bench host (downloads binaries
# into apps/speld_meet/vendor/ and writes Procfile entries):
bench --site <site> execute speld_meet.setup.install_jitsi.install_jitsi
bench setup supervisor                                     # FC: dashboard step
sudo supervisorctl reread && sudo supervisorctl update     # FC: dashboard step
```

On Frappe Cloud, the `install_jitsi` call runs from FC's Bench Console
(Site → Console → Bench Console). The supervisor reload and the
`/meet/*` nginx routes need to happen via the FC dashboard's Custom
Nginx Config field — see the docstring on
`speld_meet.setup.install_jitsi.install_jitsi` for the exact snippet.

## What's in the box

- `Meeting` DocType — room slug, host, participants, scheduled time,
  status, duration. Mints a per-user JWT signed with the in-site
  `jitsi_jwt_secret` for the local Prosody to accept.
- `/meet/<room_slug>` SPA — serves Jitsi's vendored `external_api.js`
  pointed at the same host. The Frappe page-render check enforces
  `required_roles` server-side before issuing the JWT.
- `speld_meet.setup.install_jitsi` — idempotent installer that lays down
  Prosody (source build), Jicofo + JVB (jars extracted from the upstream
  `.deb`), and configures the system Coturn. Adds Procfile entries; safe
  to re-run.
- `speld_meet.appointment_hooks` — when frappe_appointment is installed,
  a confirmed booking auto-creates a Meeting and the calendar invite
  carries the `/meet/<slug>` URL.

## Operational notes

- Pinned versions of every upstream binary live in `setup/install_jitsi.py`
  as module-level constants with SHA-256 hashes. Bumping a version is a
  one-line edit + re-run of the installer.
- Logs flow into `logs/bench-prosody.log`, `logs/bench-jicofo.log`,
  `logs/bench-jvb.log`, `logs/bench-coturn.log`.
- The Procfile lines look like any other bench worker; `bench restart`
  cycles them, `bench stop` shuts them down.

## License

AGPL-3.0. Upstream Jitsi binaries are Apache-2.0, the `external_api.js`
bundle is Apache-2.0, Prosody is MIT. All compatible with this app's
license.
