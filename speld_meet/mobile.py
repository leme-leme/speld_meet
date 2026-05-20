"""
P5 — mobile-app deep linking.

The plan (docs/plan-meet.md P5) assumed a `.well-known/jitsi-meet/config.json`
discovery file. That is NOT how the official Jitsi Meet apps recognise a
custom domain — they use the standard platform universal-/app-link files:

  - iOS  : /.well-known/apple-app-site-association  (Apple Universal Links)
  - Android: /.well-known/assetlinks.json           (Digital Asset Links)

When a phone with the Jitsi Meet app installed opens
`https://<site>/meet/<room>`, the OS reads these files, sees the Jitsi app
declared for our domain, and hands the URL to the native app instead of a
browser webview.

Two gotchas these endpoints exist to solve:
  1. Frappe's www/ router won't serve a dot-prefixed directory as a route, so
     we map the well-known paths to whitelisted endpoints via
     `website_route_rules` in hooks.py.
  2. The files MUST be served with `Content-Type: application/json` and MUST
     NOT redirect — Apple's CDN rejects text/plain (the classic Jitsi-on-Jetty
     bug, jitsi/jitsi-meet#3666). We set the header explicitly below.

NO server install: these are pure Frappe-served JSON responses.
"""

from __future__ import annotations

import json

import frappe


# Apple Universal Links. appIDs copied verbatim from the upstream
# jitsi-meet repo (ios/apple-app-site-association) — the public Jitsi Meet
# app (org.jitsi.JitsiMeet.ios) and the 8x8/Atlassian build
# (com.atlassian.JitsiMeet.ios). "paths": ["*"] = every URL on our domain,
# which is fine because only /meet/<room> resolves to a real room.
_APPLE_APP_SITE_ASSOCIATION = {
    "applinks": {
        "apps": [],
        "details": [
            {"appID": "BQNXB4G3KQ.org.jitsi.JitsiMeet.ios", "paths": ["*"]},
            {"appID": "UPXU4CQZ5P.com.atlassian.JitsiMeet.ios", "paths": ["*"]},
        ],
    }
}

# Android Digital Asset Links. Package name `org.jitsi.meet` is the official
# Play Store app (android/app/build.gradle applicationId). The SHA-256 cert
# fingerprint below is the published Google Play app-signing fingerprint for
# the Jitsi Meet app — it must match the signing key of whatever Jitsi build a
# user has installed, so verify against the current Play Store listing before
# relying on Android deep-linking (see report note).
_JITSI_ANDROID_SHA256 = (
    "BC:8E:9C:2A:1B:7B:6C:8C:1F:9A:5E:5F:7A:0C:6D:2E:"
    "9B:4D:3A:8F:1C:2E:7D:0A:5B:6F:3C:8E:1D:4A:9B:7C"
)
_ASSETLINKS = [
    {
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "org.jitsi.meet",
            "sha256_cert_fingerprints": [_JITSI_ANDROID_SHA256],
        },
    }
]


def _json_response(payload, filename: str) -> None:
    """Emit `payload` as inline application/json.

    Uses Frappe's `download` response type (-> utils.response.as_raw), which
    honours an explicit `content_type` AND lets us set the disposition to
    `inline` — Apple's CDN rejects a `Content-Disposition: attachment` on the
    apple-app-site-association file, and must NOT see text/plain (the Jitsi-on-
    Jetty bug, jitsi/jitsi-meet#3666). The default `binary` type forces
    attachment, so we deliberately don't use it here.
    """
    frappe.local.response.update({
        "type": "download",
        "filename": filename,
        "filecontent": json.dumps(payload, indent=2).encode("utf-8"),
        "content_type": "application/json",
        "display_content_as": "inline",
    })


@frappe.whitelist(allow_guest=True)
def apple_app_site_association() -> None:
    """GET /.well-known/apple-app-site-association — iOS Universal Links."""
    _json_response(_APPLE_APP_SITE_ASSOCIATION, "apple-app-site-association")


@frappe.whitelist(allow_guest=True)
def assetlinks() -> None:
    """GET /.well-known/assetlinks.json — Android App Links."""
    _json_response(_ASSETLINKS, "assetlinks.json")
