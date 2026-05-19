"""
frappe_appointment integration.

Bound from `doc_events["Appointment"]["after_insert"]` in hooks.py. When
frappe_appointment isn't installed on the bench, the hook still gets
registered but never fires (Frappe only invokes `doc_events` handlers
for doctypes that actually exist). The internal helpers still guard
against missing fields because the upstream Appointment schema has shifted
between rtCamp's tagged versions.
"""

from __future__ import annotations

import re

import frappe


def create_meeting_for_appointment(doc, method=None) -> None:
    """When a confirmed Appointment is created, mint a Meeting record.

    The handler is best-effort: if frappe_appointment's Appointment doctype
    isn't installed (yet/anymore), or if the booking is still tentative
    rather than confirmed, we no-op. This keeps the after_insert hook from
    breaking unrelated Appointment writes.
    """
    # frappe_appointment may be uninstalled but the hook still cached —
    # bail without throwing.
    if not frappe.db.exists("DocType", "Appointment Group"):
        return

    # Only act on confirmed bookings. The exact field name varies between
    # rtCamp versions; check the most common shapes and fall through if
    # neither is present (i.e. the schema changed and we should refuse to
    # guess).
    status = getattr(doc, "status", None) or getattr(doc, "appointment_status", None)
    if status and status.lower() not in ("confirmed", "booked", "accepted"):
        return

    host = getattr(doc, "user", None) or getattr(doc, "assigned_to", None)
    invitee_email = (
        getattr(doc, "customer_email", None)
        or getattr(doc, "invitee_email", None)
        or getattr(doc, "email", None)
    )
    start_time = (
        getattr(doc, "starts_on", None)
        or getattr(doc, "appointment_time", None)
        or getattr(doc, "start_time", None)
    )

    if not host:
        # No clear way to attribute → don't create an orphan Meeting.
        return

    slug = _slug_from_appointment(doc)

    # Idempotency: re-firing the hook (or a manual re-trigger) should never
    # produce duplicate Meetings for the same Appointment.
    if frappe.db.exists("Meeting", slug):
        return

    participants = [{"user": host, "role": "host"}]
    if invitee_email and frappe.db.exists("User", invitee_email):
        participants.append({"user": invitee_email, "role": "attendee"})

    try:
        meeting = frappe.get_doc({
            "doctype": "Meeting",
            "room_slug": slug,
            "title": getattr(doc, "subject", None) or f"Appointment with {host}",
            "host": host,
            "scheduled_time": start_time,
            "status": "Scheduled",
            "participants": participants,
        })
        meeting.insert(ignore_permissions=True)
        frappe.db.commit()

        # Stash the slug on the Appointment so frappe_appointment's calendar-
        # invite template can read it back. The field may not exist on
        # upstream; setattr + db_set both used so we cover both schema shapes.
        try:
            doc.db_set("meet_room_slug", slug, update_modified=False)
        except Exception:
            # If the field isn't on Appointment, leave it — the helper
            # `get_meeting_url_for_appointment` re-derives the slug.
            pass
    except Exception as exc:
        # Don't break the parent Appointment save just because Meeting
        # creation failed. Log + continue.
        frappe.log_error(
            title="speld_meet: Meeting creation from Appointment failed",
            message=f"appointment={doc.name} err={exc!r}",
        )


@frappe.whitelist()
def get_meeting_url_for_appointment(appointment: str) -> str:
    """Return the absolute /meet/<slug> URL for an Appointment.

    Used by frappe_appointment's calendar-invite template. Falls back to
    deriving the slug if the Appointment doesn't carry one — handy when
    Meetings are being backfilled for already-booked Appointments.
    """
    if not frappe.db.exists("DocType", "Appointment"):
        frappe.throw("frappe_appointment is not installed on this site")
    doc = frappe.get_doc("Appointment", appointment)
    slug = getattr(doc, "meet_room_slug", None) or _slug_from_appointment(doc)
    if not frappe.db.exists("Meeting", slug):
        # The Appointment exists but no Meeting yet — create one now using
        # the same code path as after_insert. Safer than returning a 404 URL.
        create_meeting_for_appointment(doc)
    base = frappe.utils.get_url()
    return f"{base.rstrip('/')}/meet/{slug}"


# ── slug derivation ─────────────────────────────────────────────────────────


_SLUG_SANITISE_RE = re.compile(r"[^a-z0-9-]+")


def _slug_from_appointment(doc) -> str:
    """Stable slug from an Appointment's name/subject. Mirrors how Calendly
    builds /m/<random> URLs — predictable enough to debug, opaque enough
    to not leak invitee info.

    Falls back to the Frappe name (`APPT-...`) lowercased + sanitised."""
    raw = (getattr(doc, "name", None) or "meet-appt").lower()
    slug = _SLUG_SANITISE_RE.sub("-", raw).strip("-")
    if len(slug) < 3:
        slug = f"meet-{slug or 'appt'}"
    return slug[:64]
