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

# Map `/meet/<room_slug>` directly to the dynamic SPA template. The
# generic `meet/<room_slug>.html` template handles the role-check +
# JWT-mint server-side at render time so the page ships with the
# user-scoped token already embedded.
website_route_rules = [
    {"from_route": "/meet/<room_slug>", "to_route": "meet"},
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
        "logo": "/assets/frappe/icons/timeless/video.svg",
        "title": "Meet",
        "route": "/app/meeting",
    },
]
