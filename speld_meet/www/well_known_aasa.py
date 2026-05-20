"""
www page backing /.well-known/apple-app-site-association (iOS Universal
Links), reached via the `website_route_rules` rewrite in hooks.py.

The colocated template is just `{{ aasa_json }}` with no base template, so
the response body is the bare JSON — no HTML chrome. Frappe's website
renderer serves an extension-less request path as text/html; most iOS
versions accept that, but the strict content-type (application/json) is
available via the whitelisted twin `speld_meet.mobile.apple_app_site_association`
at /api/method/... for clients/proxies that need it. See speld_meet.mobile
for the full P5 rationale (the plan's `.well-known/jitsi-meet/config.json`
was wrong — the real mechanism is platform universal-/app-links).
"""

from __future__ import annotations

import json

from speld_meet.mobile import _APPLE_APP_SITE_ASSOCIATION

no_cache = 1
base_template_path = ""  # no web.html wrapper — emit the raw JSON body


def get_context(context):
    context.aasa_json = json.dumps(_APPLE_APP_SITE_ASSOCIATION, indent=2)
    context.safe_render = False  # don't let Jinja choke on the JSON braces
    return context
