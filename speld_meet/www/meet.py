"""
Page controller for `/meet/<room_slug>`.

Wired via `website_route_rules` in hooks.py — Frappe rewrites
`/meet/<x>` → `/meet?room_slug=<x>` before calling get_context. We mint
the JWT here (after running the per-room role check) so the iframe-API
JS can hand it straight to Prosody at WebSocket-connect time.

Anonymous users get bounced to /login with `?redirect-to=/meet/<slug>`
so the post-login flow returns them to the room.
"""

from __future__ import annotations

import frappe

from speld_meet.controllers import _check_room_access, mint_jwt


# Frappe expects this on every www/ template so the renderer knows the
# route should be permitted server-side.
no_cache = 1


def get_context(context):
    room_slug = frappe.form_dict.get("room_slug") or frappe.local.form_dict.get("room_slug")
    if not room_slug:
        frappe.throw("Missing room slug", frappe.PermissionError)

    if frappe.session.user == "Guest":
        # Send anonymous users through Frappe's login first; the role-check
        # below requires a real user.
        frappe.local.flags.redirect_location = f"/login?redirect-to=/meet/{room_slug}"
        raise frappe.Redirect

    if not frappe.db.exists("Meeting", room_slug):
        frappe.throw(f"No meeting found for room '{room_slug}'", frappe.DoesNotExistError)

    meeting = frappe.get_doc("Meeting", room_slug)

    # Re-use the same access check the JWT-mint helper runs. Doing it twice
    # (once at page render, once at mint_jwt time) is intentional — the JWT
    # call here happens on the SERVER side at template render, but a future
    # AJAX-driven refresh path will hit mint_jwt directly.
    _check_room_access(meeting)

    token = mint_jwt(meeting.name)

    host_full_name = (
        frappe.db.get_value("User", meeting.host, "full_name")
        if meeting.host else None
    ) or meeting.host or "—"

    # Context shape for the Jinja template below.
    context.update({
        "meeting": meeting,
        "jwt": token["jwt"],
        "room": token["room"],
        "room_slug": token["room_slug"],
        "jitsi_host": token["domain"],
        "external_api_url": token["external_api_url"],
        "meet_mode": token["mode"],
        "host_full_name": host_full_name,
        "user_full_name": frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user,
    })
    return context
