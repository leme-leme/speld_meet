"""
Page controller for the /meet index (no room slug).

Lists the current user's upcoming + recent meetings and offers a
"New meeting" button. Anonymous users are bounced through /login first so
`my_meetings` has a real user to scope to. The actual data is fetched
client-side via `speld_meet.controllers.my_meetings` so the list stays live
without a full reload after creating an instant meeting.
"""

from __future__ import annotations

import frappe

no_cache = 1


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/meet"
        raise frappe.Redirect

    context.user_full_name = (
        frappe.db.get_value("User", frappe.session.user, "full_name")
        or frappe.session.user
    )
    return context
