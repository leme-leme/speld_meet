"""
Meeting controller.

Most of the meeting lifecycle is driven from the /meet/<room_slug> SPA via
the whitelisted helpers in `speld_meet.controllers`. This file only holds
the Frappe ORM hooks that have to live on the document class itself —
validation of `room_slug`, sensible defaults for `status`, finalize logic
on the Ended transition.
"""

from __future__ import annotations

import re

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, time_diff_in_seconds


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


class Meeting(Document):

    def validate(self):
        # Slug constraints: lowercase, dashes, 3-64 chars. Mirrors what the
        # nginx /meet/<room_slug> route accepts; tighter than Frappe's
        # default `Data` field would allow.
        if not _SLUG_RE.match(self.room_slug or ""):
            frappe.throw(
                "room_slug must be 3-64 chars of lowercase letters/digits/dash, "
                f"got {self.room_slug!r}"
            )

        # Default host = creator. The booking integration in P2 also sets
        # this explicitly; this fallback covers manual creation via /app/meeting.
        if not self.host:
            self.host = frappe.session.user

        # Default status = Scheduled. Frappe's `default` field property
        # only fires on first insert; on edits we leave whatever is there.
        if not self.status:
            self.status = "Scheduled"

    def before_save(self):
        # When the operator (or the meeting_ended controller) flips status
        # to Ended, snapshot the wall-clock duration from scheduled_time to
        # now. Cheap and inaccurate; the controllers in `controllers.py` can
        # override with a more precise value if needed.
        if (
            self.status == "Ended"
            and not self.duration_seconds
            and self.scheduled_time
        ):
            try:
                self.duration_seconds = int(
                    time_diff_in_seconds(now_datetime(), self.scheduled_time)
                )
            except Exception:  # pragma: no cover — defensive
                self.duration_seconds = 0
