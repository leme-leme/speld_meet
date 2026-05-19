from frappe.model.document import Document


class MeetingParticipant(Document):
    """Child of Meeting. No custom logic — the parent's controllers + the
    whitelisted helpers in `speld_meet.controllers` mutate `joined_at` /
    `left_at` directly via `frappe.db.set_value` on the parent."""
    pass
