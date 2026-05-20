"""
www page backing /.well-known/assetlinks.json (Android App Links), reached
via the `website_route_rules` rewrite in hooks.py.

Because the request path ends in `.json`, Frappe's website renderer guesses
`application/json` for the content-type automatically — no header juggling
needed. The whitelisted twin `speld_meet.mobile.assetlinks` mirrors this for
API callers. See speld_meet.mobile for the P5 rationale.
"""

from __future__ import annotations

import json

from speld_meet.mobile import _ASSETLINKS

no_cache = 1
base_template_path = ""  # no web.html wrapper — emit the raw JSON body


def get_context(context):
    context.assetlinks_json = json.dumps(_ASSETLINKS, indent=2)
    context.safe_render = False
    return context
