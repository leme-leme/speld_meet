app_name = "speld_meet"
app_title = "Speld Meet"
app_publisher = "De Speld"
app_description = (
    "In-bench Jitsi Meet — Meeting DocType + JWT-gated rooms + "
    "Procfile-supervised Prosody/Jicofo/JVB/Coturn. Companion to the "
    "speld app."
)
app_email = "redactie@speld.nl"
app_license = "agpl-3.0"

required_apps = ["frappe"]

# After install we DON'T auto-run the Jitsi installer — it downloads
# ~90 MB of binaries and only makes sense on a host where we can also
# write the Procfile and (manually) reload supervisor. The hook is
# triggered separately from FC's Bench Console:
#   bench --site <site> execute speld_meet.setup.install_jitsi.install_jitsi
# See speld_meet.setup.install_jitsi.install_jitsi for the full lifecycle.

# Website routes. Order matters — the exact `/meet` index is listed before
# the `/meet/<room_slug>` rule so the bare index isn't swallowed as a slug.
#
#  - /meet                → meetings index SPA (list + "New meeting")
#  - /meet/<room_slug>    → the room SPA; the template handles the role-check
#                           + JWT-mint server-side at render so the page ships
#                           with the user-scoped token already embedded.
#  - /.well-known/...      → P5 mobile deep-link files (universal/app links).
#                           Rewritten to plain-ASCII www pages because Frappe
#                           won't serve a dot-prefixed www/ directory and its
#                           static renderer refuses .json. See speld_meet.mobile.
website_route_rules = [
    {"from_route": "/meet", "to_route": "meet_index"},
    {"from_route": "/meet/<room_slug>", "to_route": "meet"},
    {
        "from_route": "/.well-known/apple-app-site-association",
        "to_route": "well_known_aasa",
    },
    {
        "from_route": "/.well-known/assetlinks.json",
        "to_route": "well_known_assetlinks",
    },
]

# Frappe_appointment integration. The handler GUARDS itself with a
# `frappe.db.exists("DocType", "Appointment Group")` check so the hook
# stays harmless on benches without frappe_appointment installed.
doc_events = {
    "Appointment": {
        "after_insert": "speld_meet.appointment_hooks.create_meeting_for_appointment",
    },
}

# Speld Meet entry on the /apps picker — self-registered. The speld app
# used to declare this entry too which made the tile appear twice on /apps;
# that duplicate was removed (see speld/hooks.py).
add_to_apps_screen = [
    {
        "name": "speld_meet",
        "logo": "/assets/speld_meet/icons/meet.svg",
        # Land on the /meet index SPA (list + New meeting), not the desk
        # doctype list — the index is the editor-facing entry point.
        "title": "Meet",
        "route": "/meet",
    },
]

# Desk navbar logo override. Raven's hooks.py sets `app_logo_url` to the
# Raven brand tile, and Frappe's hook resolution picks the LAST installed
# app's value — so we redeclare here (speld_meet is the last app in the
# manifest) to make sure the Speld brand always wins, even if install
# order ever bumps speld ahead of raven. The asset lives in the speld app
# so a bench without speld but with speld_meet still shows it (broken)
# rather than silently fall back to raven's logo.
app_logo_url = "/assets/speld/icons/speld-logo.svg"
