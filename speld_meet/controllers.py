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


# ── meeting backend selection ───────────────────────────────────────────────
#
# speld_meet works against three WebRTC backends, picked at runtime from
# site_config.json so the same app code runs everywhere:
#
#   1. "jaas"    — Jitsi-as-a-Service (8x8). Needs `jaas_app_id` +
#                  `jaas_private_key` (RS256 PEM) + optional `jaas_key_id`.
#                  Authenticated, role-gated, zero server install. Recommended.
#   2. "inbench" — self-hosted Jitsi on this bench. Needs `jitsi_jwt_secret`
#                  (HS256), written by speld_meet.setup.install_jitsi.
#   3. "public"  — meet.jit.si. No JWT, public rooms. Zero config; the
#                  fallback when neither of the above is configured so /meet
#                  rooms always carry a call.
#
# Precedence: jaas > inbench > public. The Frappe-side role check
# (_check_room_access) gates every path regardless of backend auth.

_JAAS_DOMAIN = "8x8.vc"
_PUBLIC_DOMAIN = "meet.jit.si"


def _meet_backend() -> tuple[str, str, str | None]:
    """Return (mode, jitsi_domain, app_id) from site_config."""
    conf = frappe.conf
    if conf.get("jaas_app_id") and conf.get("jaas_private_key"):
        return ("jaas", _JAAS_DOMAIN, conf.get("jaas_app_id"))
    if conf.get("jitsi_jwt_secret"):
        return ("inbench", frappe.local.site, None)
    return ("public", _PUBLIC_DOMAIN, None)


def _external_api_url(mode: str, app_id: str | None) -> str:
    """Where the page loads Jitsi's iframe-API JS from, per backend."""
    if mode == "jaas":
        return f"https://{_JAAS_DOMAIN}/{app_id}/external_api.js"
    if mode == "public":
        return f"https://{_PUBLIC_DOMAIN}/external_api.js"
    # in-bench: vendored, served same-origin so it shares the site's TLS.
    return "/assets/speld_meet/js/external_api.js"


# ── mint_jwt ───────────────────────────────────────────────────────────────


@frappe.whitelist()
def mint_jwt(meeting_name: str) -> dict:
    """Resolve the backend and issue the right token (or none) for the SPA.

    Returns:
        {
            "jwt":              <token str | None>,   # None for public mode
            "room":             <Jitsi room name>,    # backend-specific
            "room_slug":        <Frappe route slug>,  # for invite links
            "domain":           <Jitsi host>,
            "external_api_url": <iframe-API JS url>,
            "mode":             "jaas" | "inbench" | "public",
        }

    `room` differs from `room_slug`:
      - jaas:    room = "<app_id>/<slug>"  (8x8 namespaces by tenant)
      - public:  room = "speld<slug>"      (namespaced in the global meet.jit.si
                                            space; slug is already a random hash)
      - inbench: room = "<slug>"
    """
    meeting = _get_meeting(meeting_name)
    mode, domain, app_id = _meet_backend()
    slug = meeting.room_slug

    user = frappe.get_doc("User", frappe.session.user)
    user_roles = frappe.get_roles(user.name)
    is_host = frappe.session.user == meeting.host
    now = int(time.time())

    base = {
        "room_slug": slug,
        "domain": domain,
        "external_api_url": _external_api_url(mode, app_id),
        "mode": mode,
    }

    # Public meet.jit.si — no JWT. Room namespaced so our random slug doesn't
    # collide with someone else's public room. meet.jit.si strips most
    # punctuation from room names, so keep it alphanumeric.
    if mode == "public":
        room = "speld" + slug.replace("-", "")
        return {"jwt": None, "room": room, **base}

    # Lazy import — pyjwt (+ cryptography for RS256) is a pyproject dep but
    # importing at module top adds startup cost to every worker.
    import jwt

    if mode == "jaas":
        private_key = frappe.conf.get("jaas_private_key")
        key_id = frappe.conf.get("jaas_key_id") or ""
        payload = {
            "aud": "jitsi",
            "iss": "chat",
            "sub": app_id,
            # JaaS matches this against the room joined (sans tenant prefix).
            "room": slug,
            "iat": now,
            "nbf": now - 5,
            "exp": now + _JWT_TTL_SECONDS,
            "context": {
                "user": {
                    "id": user.name,
                    "name": user.full_name or user.name,
                    "email": user.email or user.name,
                    "avatar": user.user_image or "",
                    # JaaS reads moderator as a string "true"/"false".
                    "moderator": "true" if is_host else "false",
                },
                # Conservative defaults; flip on per-tenant once needed.
                "features": {
                    "livestreaming": "false",
                    "recording": "false",
                    "transcription": "false",
                    "outbound-call": "false",
                },
            },
        }
        headers = {"kid": f"{app_id}/{key_id}" if key_id else app_id, "typ": "JWT"}
        token = jwt.encode(payload, private_key, algorithm="RS256", headers=headers)
        if isinstance(token, bytes):
            token = token.decode("ascii")
        return {"jwt": token, "room": f"{app_id}/{slug}", **base}

    # in-bench HS256 (matches prosody-mod-auth-token expectations).
    secret = frappe.conf.get("jitsi_jwt_secret")
    payload = {
        "aud": "meet.speld",
        "iss": "speld_meet",
        "sub": frappe.local.site,
        "room": slug,
        "iat": now,
        "exp": now + _JWT_TTL_SECONDS,
        "context": {
            "user": {
                "id": user.name,
                "name": user.full_name or user.name,
                "email": user.email or user.name,
                "avatar": user.user_image or "",
                "roles": user_roles,
            },
            "room": {
                "required_roles": [r.role for r in (meeting.required_roles or [])],
            },
        },
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return {"jwt": token, "room": slug, **base}


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
    """Mark the current user's `left_at`. If this leaves nobody behind (every
    joined participant now has a left_at), auto-end the meeting — the SPA's
    `readyToClose`/`meeting_ended` only fires for the very last tab, which a
    crashed browser never sends. This is the defensive fallback."""
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

    # Last one out turns off the lights: if every participant who ever joined
    # has now left, end the meeting + finalize duration. Participants with no
    # joined_at (left-without-join races) don't count as "still in the room".
    still_in_room = any(
        r.joined_at and not r.left_at for r in meeting.participants
    )
    if not still_in_room and meeting.status != "Ended":
        meeting.status = "Ended"
        _finalize_duration(meeting)

    meeting.save(ignore_permissions=True)
    return {
        "meeting": meeting.name,
        "user": user,
        "left_at": str(now),
        "status": meeting.status,
    }


# ── meeting_ended ──────────────────────────────────────────────────────────


def _finalize_duration(meeting) -> None:
    """Set duration_seconds = last-leave − first-join. Falls back to
    now − first-join if some leaver was never recorded, then to
    now − scheduled_time if no joins exist at all. Mutates in place; the
    caller saves. Never raises."""
    joined = [r.joined_at for r in meeting.participants if r.joined_at]
    left = [r.left_at for r in meeting.participants if r.left_at]
    start = min(joined) if joined else meeting.scheduled_time
    # End at the latest recorded leave; if anyone left unrecorded, "now" is the
    # safer upper bound than a stale left_at.
    if left and len(left) >= len(joined) and joined:
        end = max(left)
    else:
        end = now_datetime()
    if not start:
        meeting.duration_seconds = 0
        return
    try:
        meeting.duration_seconds = max(0, int(time_diff_in_seconds(end, start)))
    except Exception:  # pragma: no cover — defensive
        meeting.duration_seconds = 0


@frappe.whitelist()
def meeting_ended(meeting_name: str) -> dict:
    """Flip status to Ended and finalize duration. Fired by the SPA's
    `readyToClose` Jitsi event (last person to leave)."""
    meeting = _get_meeting(meeting_name)

    if meeting.status == "Ended":
        return {"meeting": meeting.name, "status": "Ended", "noop": True}

    meeting.status = "Ended"
    _finalize_duration(meeting)

    meeting.save(ignore_permissions=True)
    return {
        "meeting": meeting.name,
        "status": "Ended",
        "duration_seconds": meeting.duration_seconds,
    }


# ── chat archive (P6) ───────────────────────────────────────────────────────


def _raven_installed() -> bool:
    """Raven is optional — every reference to it goes through this guard so a
    Raven-less site degrades to the Meeting Chat child table."""
    return frappe.db.exists("DocType", "Raven Channel")


@frappe.whitelist()
def ensure_raven_channel(meeting_name: str) -> dict:
    """Lazily create (once) a Raven channel for this meeting and stash its id
    on Meeting.raven_channel. No-op + empty return if Raven isn't installed —
    callers then fall back to the child table.

    Why lazy: most meetings never produce chat worth a whole Raven channel.
    We only spend the channel on first message via archive_chat_message().
    """
    meeting = _get_meeting(meeting_name)
    if not _raven_installed():
        return {"raven_channel": None, "raven": False}

    if meeting.get("raven_channel"):
        return {"raven_channel": meeting.raven_channel, "raven": True}

    try:
        # Raven's channel name must be unique + slug-ish. Reuse the room slug,
        # prefixed so it doesn't collide with hand-made channels.
        channel_name = f"meet-{meeting.room_slug}"[:140]
        channel = frappe.get_doc({
            "doctype": "Raven Channel",
            "channel_name": channel_name,
            # Private so chat from a role-gated room isn't world-readable in
            # the Raven sidebar. type is Raven's Select field.
            "type": "Private",
        })
        channel.insert(ignore_permissions=True)
        meeting.db_set("raven_channel", channel.name, update_modified=False)
        frappe.db.commit()
        return {"raven_channel": channel.name, "raven": True}
    except Exception as exc:  # pragma: no cover — defensive
        # Channel creation failed (schema drift, perms) — leave raven_channel
        # empty so the caller falls back to the child table rather than 500.
        frappe.log_error(
            title="speld_meet: ensure_raven_channel failed",
            message=f"meeting={meeting_name} err={exc!r}",
        )
        return {"raven_channel": None, "raven": False}


@frappe.whitelist()
def archive_chat_message(
    meeting_name: str, sender: str, message: str, timestamp: str | None = None
) -> dict:
    """Persist one in-room chat message past the call.

    Strategy: if Raven is installed, write into the meeting's linked Raven
    channel (creating it on first message). Otherwise append a row to the
    Meeting Chat child table. Everything is wrapped so a chat write never
    breaks the live call — worst case the message just isn't archived.
    """
    if not (message or "").strip():
        return {"archived": False, "reason": "empty"}

    meeting = _get_meeting(meeting_name)
    ts = timestamp or str(now_datetime())

    # Preferred path: Raven channel.
    if _raven_installed():
        try:
            info = ensure_raven_channel(meeting_name)
            channel_id = info.get("raven_channel")
            if channel_id and frappe.db.exists("DocType", "Raven Message"):
                msg = frappe.get_doc({
                    "doctype": "Raven Message",
                    "channel_id": channel_id,
                    # Prefix the sender into the body — Raven attributes
                    # messages to the posting User, which here is whoever the
                    # SPA call runs as, not the original Jitsi nick.
                    "text": f"<b>{frappe.utils.escape_html(sender)}:</b> "
                            f"{frappe.utils.escape_html(message)}",
                    "message_type": "Text",
                })
                msg.insert(ignore_permissions=True)
                frappe.db.commit()
                return {"archived": True, "store": "raven", "channel": channel_id}
        except Exception as exc:  # pragma: no cover — defensive
            # Fall through to the child table rather than dropping the message.
            frappe.log_error(
                title="speld_meet: Raven chat archive failed — using child table",
                message=f"meeting={meeting_name} err={exc!r}",
            )

    # Fallback path: Meeting Chat child table.
    try:
        meeting.append("chat_log", {
            "sender": sender,
            "message": message,
            "timestamp": ts,
        })
        meeting.save(ignore_permissions=True)
        return {"archived": True, "store": "child_table"}
    except Exception as exc:  # pragma: no cover — defensive
        frappe.log_error(
            title="speld_meet: chat archive failed",
            message=f"meeting={meeting_name} err={exc!r}",
        )
        return {"archived": False, "reason": "error"}


# ── instant meeting (UX) ────────────────────────────────────────────────────


@frappe.whitelist()
def create_instant_meeting(title: str | None = None) -> dict:
    """Create a Meeting hosted by the current user and return its room URL.

    Backs the "New meeting" button on the /meet index. room_slug is a short
    random token (room URLs are unguessable; the role-check still gates join).
    """
    if frappe.session.user == "Guest":
        frappe.throw("Sign in to start a meeting", frappe.PermissionError)

    slug = f"m-{frappe.generate_hash(length=10).lower()}"
    meeting = frappe.get_doc({
        "doctype": "Meeting",
        "room_slug": slug,
        "title": title or f"Meeting — {frappe.utils.now_datetime():%Y-%m-%d %H:%M}",
        "host": frappe.session.user,
        "scheduled_time": now_datetime(),
        "status": "Scheduled",
        "participants": [{"user": frappe.session.user, "role": "host"}],
    })
    meeting.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"meeting": meeting.name, "room_slug": slug, "url": f"/meet/{slug}"}


# ── meetings index data (UX) ────────────────────────────────────────────────


@frappe.whitelist()
def my_meetings(limit: int = 50) -> dict:
    """Meetings the current user can see, split into upcoming vs. recent.

    "Visible" = hosted by the user, OR they're a participant, OR the room is
    role-gated and they hold a required role. We keep it cheap: pull the
    user's hosted + participated rooms, then filter the rest by role at the
    Python level (meeting counts are small).
    """
    user = frappe.session.user
    if user == "Guest":
        frappe.throw("Sign in to see your meetings", frappe.PermissionError)

    # Rooms hosted by the user or where they appear as a participant.
    hosted = set(frappe.get_all(
        "Meeting", filters={"host": user}, pluck="name", limit_page_length=limit * 4
    ))
    joined = set(frappe.get_all(
        "Meeting Participant",
        filters={"user": user, "parenttype": "Meeting"},
        pluck="parent",
        limit_page_length=limit * 4,
    ))
    names = hosted | joined

    if not names:
        return {"upcoming": [], "recent": []}

    rows = frappe.get_all(
        "Meeting",
        filters={"name": ["in", list(names)]},
        fields=["name", "room_slug", "title", "host", "scheduled_time",
                "status", "duration_seconds"],
        order_by="scheduled_time desc",
        limit_page_length=limit,
    )

    upcoming, recent = [], []
    for r in rows:
        item = {
            "room_slug": r.room_slug,
            "title": r.title or r.room_slug,
            "host": r.host,
            "scheduled_time": str(r.scheduled_time or ""),
            "status": r.status,
            "duration_seconds": r.duration_seconds or 0,
            "url": f"/meet/{r.room_slug}",
        }
        (upcoming if r.status in ("Scheduled", "Active") else recent).append(item)

    return {"upcoming": upcoming, "recent": recent}
