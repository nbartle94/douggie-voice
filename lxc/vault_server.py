#!/usr/bin/env python3
"""
Vault HTTP server — serves nick-vault context to RunPod workers over Tailscale.
Binds to Tailscale IP 100.105.129.77:9876
GET /vault  → concatenated markdown of priority vault files (~30k tokens)
"""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

VAULT_ROOT = Path(__file__).parent.parent.parent  # /root/nick-vault
BIND_HOST  = "100.105.129.77"
BIND_PORT  = 9876

# Top-level dirs skipped entirely
SKIP_DIRS = {"999 Archive", "800 System"}

# Individual files too large or too raw to be useful in LLM context
SKIP_FILES = {
    "200 Legal/SMS History Nick & Danielle LNC684-2025.md",  # 523k chars — raw SMS dump
    "200 Legal/Court File LNC684-2025.md",                   # 7.7k — raw court docket
    "200 Legal/Medical Evidence Bundle LNC684-2025.md",      # 5k — raw evidence list
    "200 Legal/Bartle Bankruptcy Annulment Case Brief TAS95-2025.md",  # 17.5k — raw brief
    "900 Inbox/2026-05-17 - Nick Bartle Affidavit LNC684-2025.md",
    "900 Inbox/2026-05-17 - Jillian Bartle Affidavit LNC684-2025.md",
    "900 Inbox/2026-05-17 - Mention Notice LNC684-2025 FAM5358987.md",
}

# Always prepend these in order (identity + key people first)
ALWAYS_INCLUDE = [
    "000 Dashboard/000 Home.md",
    "000 Dashboard/morning_briefing.md",
    "800 System/Douggie/Douggie.md",
    "100 People/Nick Bartle.md",
    "100 People/Nick Bartle Personal.md",
    "100 People/Sophie.md",
    "100 People/Lily.md",
]


def _collect_files() -> list[Path]:
    files: list[Path] = []

    for rel in ALWAYS_INCLUDE:
        p = VAULT_ROOT / rel
        if p.exists():
            files.append(p)

    for md in sorted(VAULT_ROOT.rglob("*.md")):
        rel = md.relative_to(VAULT_ROOT)
        if rel.parts[0] in SKIP_DIRS:
            continue
        if "Templates" in rel.parts:
            continue
        if str(rel) in SKIP_FILES:
            continue
        if md in files:
            continue
        files.append(md)

    return files


def _build_payload() -> str:
    parts = []
    total = 0
    for path in _collect_files():
        try:
            content = path.read_text(encoding="utf-8").strip()
            rel = str(path.relative_to(VAULT_ROOT))
            parts.append(f"<!-- {rel} -->\n{content}")
            total += len(content)
        except Exception:
            pass
    print(f"[vault] serving {len(parts)} files, {total:,} chars (~{total//4:,} tokens)")
    return "\n\n---\n\n".join(parts)


class VaultHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path != "/vault":
            self.send_response(404)
            self.end_headers()
            return
        payload = _build_payload().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    server = HTTPServer((BIND_HOST, BIND_PORT), VaultHandler)
    print(f"Vault server listening on http://{BIND_HOST}:{BIND_PORT}/vault")
    server.serve_forever()
