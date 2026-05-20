# Meet backends

`speld_meet` renders the same `/meet/<slug>` SPA against one of three
WebRTC backends, chosen at runtime from `site_config.json`. Precedence:
**jaas → inbench → public**. The Frappe-side role check
(`_check_room_access`) gates every backend regardless of its own auth.

| Backend | `site_config` keys | Auth | Server install | Cost |
|---|---|---|---|---|
| `jaas` | `jaas_app_id`, `jaas_private_key`, `jaas_key_id` | JWT (RS256) | none | free tier, then paid |
| `inbench` | `jitsi_jwt_secret` | JWT (HS256) | `install_jitsi` | infra only |
| `public` | *(none)* | none — public rooms | none | free |

With no keys set, `/meet` rooms run on **meet.jit.si** (public) so calls
work out of the box.

## Enabling JaaS (recommended)

1. Create a free JaaS tenant at <https://jaas.8x8.vc>. Note the **AppID**
   (`vpaas-magic-cookie-…`).
2. In the JaaS console → **API Keys** → add a key pair. Download the
   **private key** (PEM) and copy the **Key ID** (the kid).
3. Put the three values in the site config (via FC dashboard → Site →
   Config, or the API):

   ```
   jaas_app_id     = vpaas-magic-cookie-xxxxxxxxxxxx
   jaas_key_id     = vpaas-magic-cookie-xxxxxxxxxxxx/abc123   # the kid
   jaas_private_key = -----BEGIN PRIVATE KEY-----\n…\n-----END PRIVATE KEY-----
   ```

   `jaas_private_key` is the full PEM (newlines as `\n` in JSON is fine).
4. Clear the site cache. Done — `/meet` rooms now run on `8x8.vc` with
   per-user JWTs; the meeting host gets `moderator: true`.

To roll back to public rooms, remove the three `jaas_*` keys.

## Notes

- The JWT `room` claim is the bare room slug; the iframe joins
  `<app_id>/<slug>` (8x8 namespaces rooms by tenant).
- `recording` / `livestreaming` / `transcription` features are disabled
  in the JWT by default — enable per-tenant in `controllers.mint_jwt`
  once the JaaS plan allows them.
- `cryptography` (pulled in by PyJWT for RS256) must be in the build —
  it is, transitively, via Frappe.
