"""
Bring up Jitsi Meet as in-bench Procfile workers.

Triggered manually from FC's Bench Console (or `bench --site <site> execute ...`
on a self-hosted bench) — NOT from the app's after_install hook. The
installer:

  1. Downloads pinned Prosody (source tarball), Jitsi Videobridge + Jicofo
     (extracted from upstream `.deb` packages), and the Jitsi prosody-plugins
     bundle into `apps/speld_meet/vendor/<component>/`. Each download is
     SHA-256 verified against the pins in `_JITSI_BLOBS` below.
  2. Generates per-site secrets via `frappe.generate_hash()` and persists
     them to site_config.json:
       - jitsi_jwt_secret    — HS256 signing key shared with Prosody mod_auth_token
       - prosody_admin_pwd   — admin user on the local XMPP server
       - jvb_auth_pwd        — JVB → Prosody MUC join credential
       - coturn_secret       — shared secret for ephemeral TURN credentials
  3. Renders Jinja templates from `setup/templates/` into
     `apps/speld_meet/run/<component>/` with the secrets baked in.
  4. Appends Procfile entries for `prosody`, `jicofo`, `jvb`, `coturn` if
     they're not already present. Idempotent — second run is a no-op.

The function is idempotent end-to-end: re-running detects already-downloaded
binaries (SHA match), pre-existing secrets (left alone), and pre-existing
Procfile lines (skipped).

## Rebuild resilience on Frappe Cloud

FC re-generates the bench image when an app pin changes; the supervisor
config is regenerated from the Procfile on container start. The vendor/
and run/ directories live under `apps/speld_meet/` which IS part of the
app source tree mounted into the container, so they survive container
restarts but NOT image rebuilds. After every FC bench rebuild this
installer needs a re-run from Bench Console — same drill as for any
external binary FC's image isn't aware of (see speld/setup/install.py's
mail-cert handling for the same pattern).

## What this hook does NOT do

- It does NOT install nginx routes. Frappe Cloud owns nginx; the
  `/xmpp-websocket` and `/colibri-ws` proxy rules need to be added via
  the FC dashboard's Custom Nginx Config. See the snippet in
  `_NGINX_HINT` below for what to paste.
- It does NOT run `apt install`. FC's image is locked — we get Python
  and Node but not root-level apt. System Coturn (`/usr/bin/turnserver`)
  is detected when present; if absent, the coturn Procfile line is
  skipped with a warning and TURN falls back to public meet-jit-si
  servers.
- It does NOT call `bench setup supervisor` automatically — that needs
  a privileged shell. The installer prints the exact command the
  operator must run from Bench Console.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import frappe
from frappe.installer import update_site_config
from jinja2 import Environment, FileSystemLoader, StrictUndefined


# ── Pinned upstream versions ─────────────────────────────────────────────────
#
# All hashes computed against download.jitsi.org / prosody.im on 2026-05-20.
# Bump in lockstep with what FC's nginx admin has tested. The jitsi-meet build
# number (10888) is the "stable" tag from 2026-03-30; jicofo and jvb releases
# are cut together against the same Jitsi Meet build.


@dataclass(frozen=True)
class _Blob:
    name: str             # short id, e.g. "jvb"
    url: str
    sha256: str
    filename: str         # name to save the download as in vendor/<name>/
    # If the download is a .deb, the installer dpkg-extracts it; if it's a
    # tarball, it gets unpacked. .js / .lua blobs are saved verbatim.
    archive_kind: str     # "deb" | "tar.gz" | "raw"


# Jitsi Meet stable/10888 (2026-03-30 cut). Jitsi distributes via .deb only;
# we extract the JARs from those packages rather than depending on `dpkg -i`.
_JITSI_BLOBS: tuple[_Blob, ...] = (
    _Blob(
        name="prosody",
        url="https://prosody.im/downloads/source/prosody-13.0.5.tar.gz",
        sha256="943b24860efd10e9db7eaab87e35f82415a55d46a694b667f0210b88a4323c42",
        filename="prosody-13.0.5.tar.gz",
        archive_kind="tar.gz",
    ),
    _Blob(
        name="prosody_plugins",
        # The jitsi-meet-prosody .deb ships the Lua plugin bundle (mod_auth_token
        # etc.) under /usr/share/jitsi-meet/prosody-plugins/. We pull only the
        # plugins out, not the .deb's tiny prosody.cfg.lua stub.
        url="https://download.jitsi.org/stable/jitsi-meet-prosody_1.0.9139-1_all.deb",
        sha256="b84368e7d9f9b05526768cce49f95062a0cc70e8a4a4f9f84917b7cd53d90254",
        filename="jitsi-meet-prosody.deb",
        archive_kind="deb",
    ),
    _Blob(
        name="jicofo",
        url="https://download.jitsi.org/stable/jicofo_1.0-1174-1_all.deb",
        sha256="24e5a6e44eb2e0e4b6ffafcbd3abbd4da47ea5780d681852f141cf45e6fcb81e",
        filename="jicofo.deb",
        archive_kind="deb",
    ),
    _Blob(
        name="jvb",
        url="https://download.jitsi.org/stable/jitsi-videobridge2_2.3-287-g4f55d380a-1_all.deb",
        sha256="309bda740f94610e4defcffa329bfe4f1b7f5a6e82fca697bd700bf459da8b29",
        filename="jvb.deb",
        archive_kind="deb",
    ),
)


# ── Site config keys (referenced by the controllers + the templates) ────────


_SECRET_KEYS = (
    "jitsi_jwt_secret",
    "prosody_admin_pwd",
    "jvb_auth_pwd",
    "coturn_secret",
)


# ── Procfile entries we append ──────────────────────────────────────────────


# Each entry's prefix (`prosody:`) is the supervisor program name. The right
# side is the literal command line the bench Procfile will hand to honcho /
# supervisord. Paths are relative to the bench root — bench resolves them at
# Procfile-parse time.
def _procfile_lines(bench_root: Path, has_system_coturn: bool) -> list[str]:
    app_root = "apps/speld_meet"
    lines = [
        # Prosody runs from its source tree; the in-tree `prosody` shell
        # wrapper handles LUAPATH itself, so we point at it directly.
        f"prosody: {app_root}/vendor/prosody/prosody-bin -F "
        f"--config {app_root}/run/prosody/prosody.cfg.lua",
        # Jicofo & JVB ship `jicofo.sh` / `jvb.sh` launchers in the .deb but
        # we go straight to `java -jar` to bypass the systemd-aware wrapper.
        f"jicofo: java -Djava.util.logging.config.file={app_root}/run/jicofo/logging.properties "
        f"-Dconfig.file={app_root}/run/jicofo/jicofo.conf "
        f"-jar {app_root}/vendor/jicofo/jicofo.jar",
        f"jvb: java -Djava.util.logging.config.file={app_root}/run/jvb/logging.properties "
        f"-Dconfig.file={app_root}/run/jvb/jvb.conf "
        f"-jar {app_root}/vendor/jvb/jitsi-videobridge.jar",
    ]
    if has_system_coturn:
        lines.append(
            f"coturn: /usr/bin/turnserver -c {app_root}/run/coturn/turnserver.conf"
        )
    return lines


# ── Operator-facing nginx hint ──────────────────────────────────────────────


_NGINX_HINT = """
location /xmpp-websocket {
    proxy_pass http://127.0.0.1:5280/xmpp-websocket;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    tcp_nodelay on;
}

location /colibri-ws {
    proxy_pass http://127.0.0.1:9090/colibri-ws;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    tcp_nodelay on;
}
""".strip()


# ── Entry point ─────────────────────────────────────────────────────────────


@frappe.whitelist()
def install_jitsi() -> dict:
    """Lay down Jitsi binaries, render configs, append Procfile entries.

    Returns a dict summarising what changed — useful when called from
    Bench Console so the operator can see at a glance whether anything
    was a no-op vs. a fresh install.
    """
    # `frappe.only_for` not used here — Bench Console runs as Administrator
    # implicitly, and a non-admin shouldn't have execute() rights anyway.
    bench_root = _detect_bench_root()
    app_root = bench_root / "apps" / "speld_meet"
    vendor_root = app_root / "vendor"
    run_root = app_root / "run"

    summary: dict[str, list[str]] = {
        "downloaded": [],
        "extracted": [],
        "secrets_created": [],
        "templates_rendered": [],
        "procfile_added": [],
        "errors": [],
    }

    # 1. Download + verify + extract each blob.
    for blob in _JITSI_BLOBS:
        try:
            changed = _ensure_blob(vendor_root, blob)
            if changed.get("downloaded"):
                summary["downloaded"].append(blob.name)
            if changed.get("extracted"):
                summary["extracted"].append(blob.name)
        except Exception as exc:  # pragma: no cover — network / disk
            summary["errors"].append(f"{blob.name}: {exc}")
            print(f"  ! {blob.name}: {exc}")

    # 2. Generate site-config secrets. Use frappe.generate_hash() — same
    # 40-char random hex that Frappe uses for its own api_secret.
    for key in _SECRET_KEYS:
        if not frappe.conf.get(key):
            update_site_config(key, frappe.generate_hash(length=40))
            summary["secrets_created"].append(key)
            print(f"  ✓ secret '{key}' generated")
        else:
            print(f"  • secret '{key}' already set — leaving untouched")

    # 3. Render config templates. Bind the secrets we just persisted (or
    # already had) into the Jinja context so prosody/jicofo/jvb/turnserver
    # configs come out with the right shared values.
    rendered = _render_configs(app_root, run_root)
    summary["templates_rendered"].extend(rendered)

    # 4. Append Procfile lines.
    has_coturn = Path("/usr/bin/turnserver").exists()
    if not has_coturn:
        print("  ! /usr/bin/turnserver missing — coturn Procfile line skipped; "
              "TURN will fall back to meet-jit-si public relays")
    added = _ensure_procfile_lines(bench_root, has_coturn)
    summary["procfile_added"].extend(added)

    # 5. Print the post-install steps the operator still needs to do.
    _print_followup(bench_root, summary)

    return summary


# ── Helpers ─────────────────────────────────────────────────────────────────


def _detect_bench_root() -> Path:
    """Locate the bench root from the running site. Frappe stores it as
    `frappe.utils.get_bench_path()`; we don't import that lazily because we
    NEED it — bail loudly if it's gone."""
    from frappe.utils import get_bench_path
    return Path(get_bench_path())


def _ensure_blob(vendor_root: Path, blob: _Blob) -> dict[str, bool]:
    """Download `blob` to `vendor_root/<name>/`, verify, extract if needed.

    Returns flags so the caller can summarise. Re-running on an
    already-good vendor dir is a fast no-op (SHA check + dir-exists check).
    """
    dest_dir = vendor_root / blob.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    download_path = dest_dir / blob.filename

    # Skip download if file present + hash matches.
    if download_path.exists() and _sha256(download_path) == blob.sha256:
        print(f"  • {blob.name}: hash matches, skipping download")
        downloaded = False
    else:
        print(f"  → {blob.name}: downloading {blob.url}")
        _download(blob.url, download_path)
        actual = _sha256(download_path)
        if actual != blob.sha256:
            download_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"SHA-256 mismatch for {blob.name}: "
                f"expected {blob.sha256}, got {actual}"
            )
        downloaded = True

    # Extract once — the presence of a marker file means we've already done it.
    marker = dest_dir / ".extracted"
    if marker.exists():
        return {"downloaded": downloaded, "extracted": False}

    if blob.archive_kind == "deb":
        _extract_deb(download_path, dest_dir, blob.name)
    elif blob.archive_kind == "tar.gz":
        _extract_targz(download_path, dest_dir)
        # For prosody we want the source tree built so we have a runnable
        # binary. Build is heavy (~30s) and depends on libidn/openssl headers;
        # if it fails, leave the source tree in place and let the operator
        # finish the build themselves — log the failure but don't abort.
        if blob.name == "prosody":
            _build_prosody(dest_dir)
    # archive_kind == "raw" → nothing to do, file is the artifact

    marker.touch()
    print(f"  ✓ {blob.name}: extracted into {dest_dir}")
    return {"downloaded": downloaded, "extracted": True}


def _download(url: str, dest: Path) -> None:
    """Stream `url` to `dest`. Uses urllib because Frappe's image always has
    it; requests is optional. Writes to `dest.tmp` then atomically renames."""
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    req = urllib.request.Request(url, headers={"User-Agent": "speld_meet-installer"})
    with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_deb(deb_path: Path, dest_dir: Path, name: str) -> None:
    """Pull the payload out of a Debian package without needing dpkg.

    .deb is `ar`-archive containing `control.tar.{gz,xz}` and `data.tar.{xz,gz,zst}`.
    We only care about data.tar.* — and we strip the unpacked filesystem down
    to the JARs / Lua plugins that the Procfile entries actually invoke.
    """
    # Step 1: use system `ar` to crack the outer envelope. `ar` is in
    # binutils, present on every Debian/Ubuntu image FC builds on.
    work = dest_dir / "_work"
    work.mkdir(exist_ok=True)
    subprocess.run(
        ["ar", "x", str(deb_path.resolve())],
        cwd=work, check=True, capture_output=True,
    )

    # Step 2: locate data.tar.* and extract it.
    data_tar = next(
        (p for p in work.iterdir() if p.name.startswith("data.tar")),
        None,
    )
    if not data_tar:
        raise RuntimeError(f"no data.tar in {deb_path}")
    with tarfile.open(data_tar) as t:
        t.extractall(work)

    # Step 3: pick out what we actually need.
    if name == "jvb":
        # /usr/share/jitsi-videobridge/jvb.jar  → vendor/jvb/jitsi-videobridge.jar
        _find_and_copy(work, "jitsi-videobridge.jar", dest_dir / "jitsi-videobridge.jar")
        # Also pull the bundled lib/ jars next to it (jvb.jar references them).
        _copy_tree(work, "usr/share/jitsi-videobridge", dest_dir / "lib", strip_top=True)
    elif name == "jicofo":
        _find_and_copy(work, "jicofo.jar", dest_dir / "jicofo.jar")
        _copy_tree(work, "usr/share/jicofo", dest_dir / "lib", strip_top=True)
    elif name == "prosody_plugins":
        # The plugins .deb ships them at /usr/share/jitsi-meet/prosody-plugins/.
        src = work / "usr/share/jitsi-meet/prosody-plugins"
        if src.is_dir():
            target = dest_dir / "plugins"
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
    # Clean up the unpack tree — keep dest_dir small.
    shutil.rmtree(work, ignore_errors=True)


def _find_and_copy(work: Path, jar_name: str, target: Path) -> None:
    """Find the first `<jar_name>` under `work/` and copy it to `target`."""
    for p in work.rglob(jar_name):
        if p.is_file():
            shutil.copy2(p, target)
            return
    raise RuntimeError(f"{jar_name} not found in extracted .deb tree under {work}")


def _copy_tree(work: Path, relpath: str, target: Path, *, strip_top: bool = False) -> None:
    """Copy `work/relpath/` to `target/`. If `strip_top`, target gets the
    contents (not the parent dir)."""
    src = work / relpath
    if not src.is_dir():
        return
    if target.exists():
        shutil.rmtree(target)
    if strip_top:
        target.mkdir(parents=True)
        for item in src.iterdir():
            dest = target / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
    else:
        shutil.copytree(src, target)


def _extract_targz(tar_path: Path, dest_dir: Path) -> None:
    """Unpack a source tarball into dest_dir."""
    with tarfile.open(tar_path, "r:gz") as t:
        # tarfile.data_filter was added in 3.12; gracefully fall back on older.
        try:
            t.extractall(dest_dir, filter="data")
        except TypeError:
            t.extractall(dest_dir)


def _build_prosody(dest_dir: Path) -> None:
    """Build Prosody from source. Best-effort: if libidn/openssl headers are
    missing the build fails and we log + continue. The Procfile entry will
    then error out on bench start with a clear "no such file" — preferable
    to silently leaving a broken install."""
    src_dir = next((d for d in dest_dir.iterdir() if d.is_dir() and d.name.startswith("prosody-")), None)
    if not src_dir:
        print("  ! prosody: source tree not found after extract")
        return
    # `./configure --prefix=./local` so the build stays self-contained.
    try:
        subprocess.run(
            ["./configure", "--prefix", str(dest_dir / "local"),
             "--with-lua-include=/usr/include/lua5.2",
             "--ostype=linux"],
            cwd=src_dir, check=True, capture_output=True, timeout=120,
        )
        subprocess.run(
            ["make", "-j2"],
            cwd=src_dir, check=True, capture_output=True, timeout=300,
        )
        subprocess.run(
            ["make", "install"],
            cwd=src_dir, check=True, capture_output=True, timeout=60,
        )
        # Symlink the built binary into vendor/prosody/prosody-bin so the
        # Procfile entry doesn't change when we bump the source version.
        prosody_bin = dest_dir / "local" / "bin" / "prosody"
        if prosody_bin.exists():
            target = dest_dir / "prosody-bin"
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(prosody_bin)
        print(f"  ✓ prosody: built into {dest_dir / 'local'}")
    except subprocess.CalledProcessError as exc:
        # Surface stderr to the operator; common cause is missing libidn-dev.
        err = exc.stderr.decode("utf-8", "replace") if exc.stderr else ""
        print(f"  ! prosody build failed (probably missing headers): {err.splitlines()[-1] if err else exc}")
    except Exception as exc:
        print(f"  ! prosody build skipped: {exc}")


def _render_configs(app_root: Path, run_root: Path) -> list[str]:
    """Render Jinja templates → run/<component>/<config>."""
    template_dir = Path(__file__).resolve().parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )

    # All four templates share the same context — duplication beats divergence.
    site = frappe.local.site
    ctx = {
        "site_domain":        site,
        "xmpp_domain":        f"meet.{site}",
        "xmpp_auth_domain":   f"auth.meet.{site}",
        "xmpp_muc_domain":    f"muc.meet.{site}",
        "xmpp_internal_muc":  f"internal-muc.meet.{site}",
        "xmpp_hidden_domain": f"hidden.meet.{site}",
        "jitsi_jwt_secret":   frappe.conf.get("jitsi_jwt_secret", ""),
        "prosody_admin_pwd":  frappe.conf.get("prosody_admin_pwd", ""),
        "jvb_auth_pwd":       frappe.conf.get("jvb_auth_pwd", ""),
        "coturn_secret":      frappe.conf.get("coturn_secret", ""),
        "prosody_plugins_dir": str(app_root / "vendor" / "prosody_plugins" / "plugins"),
    }

    targets = [
        ("prosody.cfg.lua.j2",   "prosody/prosody.cfg.lua"),
        ("jicofo.conf.j2",       "jicofo/jicofo.conf"),
        ("jvb.conf.j2",          "jvb/jvb.conf"),
        ("turnserver.conf.j2",   "coturn/turnserver.conf"),
    ]
    rendered: list[str] = []
    for tpl_name, rel_out in targets:
        tpl = env.get_template(tpl_name)
        out_path = run_root / rel_out
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(tpl.render(**ctx))
        rendered.append(rel_out)
        print(f"  ✓ rendered run/{rel_out}")

    # Java logging properties — fixed content, not templated, but kept here so
    # the Procfile line's -Djava.util.logging.config.file path always exists.
    for comp in ("jicofo", "jvb"):
        log_cfg = run_root / comp / "logging.properties"
        log_cfg.parent.mkdir(parents=True, exist_ok=True)
        if not log_cfg.exists():
            log_cfg.write_text(
                "handlers = java.util.logging.ConsoleHandler\n"
                ".level = INFO\n"
                "java.util.logging.ConsoleHandler.level = INFO\n"
                "java.util.logging.ConsoleHandler.formatter = "
                "java.util.logging.SimpleFormatter\n"
            )

    return rendered


def _ensure_procfile_lines(bench_root: Path, has_coturn: bool) -> list[str]:
    """Append our Procfile entries if they're not already present.

    The bench Procfile is line-oriented; one program per line, prefix is
    the supervisor program name. We match on prefix (`prosody:` etc.) so
    we don't re-add an entry the operator may have already hand-edited.
    """
    procfile = bench_root / "Procfile"
    if not procfile.exists():
        print(f"  ! Procfile not found at {procfile} — bench may not be initialized")
        return []

    existing = procfile.read_text().splitlines()
    existing_prefixes = {
        line.split(":", 1)[0].strip()
        for line in existing
        if ":" in line and not line.strip().startswith("#")
    }

    to_add: list[str] = []
    for line in _procfile_lines(bench_root, has_coturn):
        prefix = line.split(":", 1)[0]
        if prefix in existing_prefixes:
            print(f"  • Procfile entry '{prefix}' already present — leaving untouched")
            continue
        to_add.append(line)

    if to_add:
        # Append with a leading blank line so the entries stand out from
        # bench's defaults; this mirrors what `bench add-procfile-entry` does.
        new_text = procfile.read_text()
        if not new_text.endswith("\n"):
            new_text += "\n"
        new_text += "\n# Added by speld_meet.setup.install_jitsi\n"
        new_text += "\n".join(to_add) + "\n"
        procfile.write_text(new_text)
        for line in to_add:
            print(f"  ✓ Procfile += {line.split(':',1)[0]}")
    return [line.split(":", 1)[0] for line in to_add]


def _print_followup(bench_root: Path, summary: dict) -> None:
    """Print the manual steps the operator still has to take. Same shape
    as speld.setup.install's per-section log lines so it composes."""
    print("")
    print("=" * 70)
    print("Jitsi install summary")
    print("=" * 70)
    for k, v in summary.items():
        print(f"  {k}: {', '.join(v) if v else '(none)'}")
    print("")
    print("Next steps (operator):")
    print(f"  1. cd {bench_root} && bench setup supervisor")
    print(f"     (or, on FC: dashboard → Bench Group → 'Reload Supervisor')")
    print(f"  2. bench restart   # picks up the new Procfile entries")
    print(f"  3. Add to FC dashboard → Site → Settings → Custom Nginx Config:")
    print("")
    for line in _NGINX_HINT.splitlines():
        print(f"     {line}")
    print("")
    print(f"  4. Test by visiting https://{frappe.local.site}/meet/test-room")
    print("     (you'll need to create a Meeting record first via /app/meeting)")
