from frappe.model.document import Document


class MeetingChat(Document):
    """Child of Meeting. Raven-less fallback store for in-room chat — one row
    per archived message. The whitelisted `archive_chat_message` helper in
    `speld_meet.controllers` appends rows here when no Raven channel is linked."""
    pass
