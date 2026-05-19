"""
Whitelisted helpers backing the /meet/<room_slug> SPA.

Four methods, all callable from the SPA via `frappe.call(...)`:

  - mint_jwt(meeting_name)         → {"jwt": "...", "room": "<slug>",
                                        "domain": "<site>"}
  - participant_joined(meeting)    → idempotent — first call sets joined_at,
                                     flips Meeting.status to Active.
  - participant_left(meeting)      → sets left_at on the current user's row.
  - meeting_ended(meeting)         → flips status to Ended, finalizes
                                     duration_seconds. Anyone in the room
                                     can call this — Jitsi's
                                     `readyToClose` event fires for the
                                     last person to leave.

All four enforce per-meeting access via `required_roles` at call time;
mint_jwt also enforces it at JWT-issue time so a non-authorized user can't
even get the token.
"""

from __future__ import annotations

import time

import frappe
from frappe.utils import now_datetime, time_diff_in_seconds


# JWT lifetime — long enough that a long-running meeting doesn't hit
# Prosody's reauth window, short enough that a leaked token can't be
# replayed days later. The Prosody mod_auth_token plugin checks `exp`
# at every WebSocket connect.
_JWT_TTL_SECONDS = 60 * 60  # 1 hour


def _check_room_access(meeting) -> None:
    """Throw PermissionError if the current user can't join this meeting.

    Empty required_roles → public to anyone the doctype itself permits
    (read access on the Meeting record is the gate then). Non-empty →
    user must hold at least one of the listed roles.
    """
    required = [r.role for r in (meeting.required_roles or [])]
    if not required:
        return
    user_roles = set(frappe.get_roles(frappe.session.user))
    if not user_roles.intersection(required):
        frappe.throw(
            f"Not allowed to join this meeting — needs one of: {', '.join(required)}",
            frappe.PermissionError,
        )


def _get_meeting(meeting_name: str):
    """Load Meeting + run permission check. Centralized so all four entry
    points use identical access logic."""
    meeting = frappe.get_doc("Meeting", meeting_name)
    _check_room_access(meeting)
    return meeting


# ── mint_jwt ───────────────────────────────────────────────────────────────


@frappe.whitelist()
def mint_jwt(meeting_name: str) -> dict:
    """Issue a short-lived HS256 JWT for the in-bench Prosody to validate.

    Claims shape (matches `prosody-mod-auth-token`'s expectations):

        {
            "aud": "meet.speld",                # asap_accepted_audiences
            "iss": "speld_meet",                # asap_accepted_issuers
            "sub": "<site host>",               # the XMPP_DOMAIN
            "room": "<room_slug>",              # meeting.room_slug
            "exp": <epoch seconds>,
            "iat": <epoch seconds>,
            "context": {
                "user": {
                    "id":     "<frappe user id (email)>",
                    "name":   "<full name>",
                    "email":  "<email>",
                    "avatar": "<gravatar url>",
                    "roles":  ["...", ...]      # current user's Frappe roles
                },
                "room": {
                    "required_roles": ["..."]   # from Meeting.required_roles
                }
            }
        }
    """
    # Lazy import — pyjwt is in the app's pyproject deps but importing at
    # module top-level adds startup cost to every Frappe worker.
    import jwt

    meeting = _get_meeting(meeting_name)
    secret = frappe.conf.get("jitsi_jwt_secret")
    if not secret:
        frappe.throw(
            "site_config.json is missing jitsi_jwt_secret — run "
            "`bench --site <site> execute speld_meet.setup.install_jitsi.install_jitsi`"
        )

    user = frappe.get_doc("User", frappe.session.user)
    user_roles = frappe.get_roles(user.name)

    now = int(time.time())
    payload = {
        "aud": "meet.speld",
        "iss": "speld_meet",
        "sub": frappe.local.site,
        "room": meeting.room_slug,
        "iat": now,
        "exp": now + _JWT_TTL_SECONDS,
        "context": {
            "user": {
                "id":     user.name,
                "name":   user.full_name or user.name,
                "email":  user.email or user.name,
                "avatar": user.user_image or "",
                "roles":  user_roles,
            },
            "room": {
                "required_roles": [r.role for r in (meeting.required_roles or [])],
            },
        },
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    # PyJWT 2.x returns str already; PyJWT 1.x returned bytes — coerce.
    if isinstance(token, bytes):
        token = token.decode("ascii")

    return {
        "jwt": token,
        "room": meeting.room_slug,
        "domain": frappe.local.site,
    }


# ── participant_joined ─────────────────────────────────────────────────────


@frappe.whitelist()
def participant_joined(meeting_name: str) -> dict:
    """Mark the current user as joined. Idempotent — repeated calls leave
    the existing joined_at alone (Jitsi fires the join event on every
    reconnect)."""
    meeting = _get_meeting(meeting_name)
    user = frappe.session.user
    now = now_datetime()

    row = next((r for r in meeting.participants if r.user == user), None)
    if row is None:
        meeting.append("participants", {
            "user": user,
            "role": "host" if user == meeting.host else "attendee",
            "joined_at": now,
        })
    elif not row.joined_at:
        row.joined_at = now

    if meeting.status == "Scheduled":
        meeting.status = "Active"

    meeting.save(ignore_permissions=True)
    return {"meeting": meeting.name, "user": user, "joined_at": str(now)}


# ── participant_left ───────────────────────────────────────────────────────


@frappe.whitelist()
def participant_left(meeting_name: str) -> dict:
    """Mark the current user's `left_at`. Does NOT end the meeting — the
    SPA fires `meeting_ended` separately when the last person leaves."""
    meeting = _get_meeting(meeting_name)
    user = frappe.session.user
    now = now_datetime()

    row = next((r for r in meeting.participants if r.user == user), None)
    if row is None:
        # Race: left event with no preceding join. Record a row anyway so the
        # audit trail isn't lossy.
        meeting.append("participants", {
            "user": user, "role": "attendee", "left_at": now,
        })
    else:
        row.left_at = now

    meeting.save(ignore_permissions=True)
    return {"meeting": meeting.name, "user": user, "left_at": str(now)}


# ── meeting_ended ──────────────────────────────────────────────────────────


@frappe.whitelist()
def meeting_ended(meeting_name: str) -> dict:
    """Flip status to Ended and finalize duration. Fired by the SPA's
    `readyToClose` Jitsi event."""
    meeting = _get_meeting(meeting_name)

    if meeting.status == "Ended":
        return {"meeting": meeting.name, "status": "Ended", "noop": True}

    meeting.status = "Ended"

    # Prefer the actual wall-clock span between the first join and now;
    # fall back to scheduled_time → now if no joins were recorded.
    joined_times = [
        r.joined_at for r in meeting.participants if r.joined_at
    ]
    start = min(joined_times) if joined_times else meeting.scheduled_time
    if start:
        try:
            meeting.duration_seconds = int(
                time_diff_in_seconds(now_datetime(), start)
            )
        except Exception:  # pragma: no cover — defensive
            meeting.duration_seconds = 0

    meeting.save(ignore_permissions=True)
    return {
        "meeting": meeting.name,
        "status": "Ended",
        "duration_seconds": meeting.duration_seconds,
    }
