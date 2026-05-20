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

# Speld Meet entry on the /apps picker. Speld owns the picker definition
# for first-party apps (see speld/hooks.py); we self-register here too so
# the app shows up on benches that have speld_meet but not the speld app.
add_to_apps_screen = [
    {
        "name": "speld_meet",
        # Frappe v15's timeless set ships no standalone video.svg — the glyphs
        # are bundled in a sprite. App-picker logos need a direct image URL, so
        # we reuse the message glyph (same fallback the speld app's newsletter
        # entry uses) until a real Meet logo asset ships.
        "logo": "/assets/frappe/icons/timeless/message.svg",
        # Land on the /meet index SPA (list + New meeting), not the desk
        # doctype list — the index is the editor-facing entry point.
        "title": "Meet",
        "route": "/meet",
    },
]
