#!/usr/bin/env python3
"""Assemble the respondent page for one environment.

  python build.py --env dev    -> docs/dev/index.html
  python build.py --env prod   -> docs/index.html
  python build.py --env local  -> test/index.local.html (the Postgres stub + fake edge)

Reads config/<env>.json, inlines src/session.css and src/session.js into
src/index.html at the <!-- inline: name --> markers (the script as a module)
and any src/ file named by an <!-- inline-base64: name --> marker (the font,
the mark) as base64, substitutes the __SUPABASE_URL__, __SUPABASE_KEY__ and
__PAGES_BASE__ tokens, and copies legacy/index.html alongside as legacy.html.
A config may name "supabase_js": the URL session.js loads supabase-js from
(the local env points it at the fake edge; dev and prod keep the CDN).
"""
import argparse, base64, json, pathlib, re, shutil, sys

ROOT = pathlib.Path(__file__).resolve().parent
SRC, LEGACY, DOCS, TEST = ROOT / "src", ROOT / "legacy", ROOT / "docs", ROOT / "test"
CDN_SUPABASE_JS = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/+esm"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["dev", "prod", "local"], required=True)
    env = ap.parse_args().env
    cfg = json.loads((ROOT / "config" / f"{env}.json").read_text())
    if "PASTE" in cfg["key"] or "PROD_PROJECT_REF" in cfg["url"]:
        sys.exit(f"config/{env}.json still has placeholders")

    page = (SRC / "index.html").read_text()
    for name in ("session.css", "session.js"):
        marker = f"<!-- inline: {name} -->"
        if marker not in page:
            sys.exit(f"src/index.html is missing {marker}")
        body = (SRC / name).read_text()
        if name.endswith(".css"):
            page = page.replace(marker, f"<style>\n{body}\n</style>")
        else:
            page = page.replace(marker, f'<script type="module">\n{body}\n</script>')

    def b64(m):
        f = SRC / m.group(1)
        if not f.exists():
            sys.exit(f"src/index.html asks for {f.name} but src/ has no such file")
        return base64.b64encode(f.read_bytes()).decode()
    page = re.sub(r"<!-- inline-base64: ([\w.-]+) -->", b64, page)

    for token in ("__SUPABASE_URL__", "__SUPABASE_KEY__"):
        if token not in page:
            sys.exit(f"{token} not found; replace the hardcoded constant in src/ with the token")
    page = (page.replace("__SUPABASE_URL__", cfg["url"])
                .replace("__SUPABASE_KEY__", cfg["key"])
                .replace("__PAGES_BASE__", cfg["pages_base"]))
    if cfg.get("supabase_js"):
        if CDN_SUPABASE_JS not in page:
            sys.exit(f"config names supabase_js but src/ no longer loads {CDN_SUPABASE_JS}")
        page = page.replace(CDN_SUPABASE_JS, cfg["supabase_js"])
    page = page.replace("</head>", f'<meta name="poll-env" content="{env}">\n</head>', 1)

    out = {"prod": DOCS / "index.html", "dev": DOCS / "dev/index.html", "local": TEST / "index.local.html"}[env]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    if env != "local" and (LEGACY / "index.html").exists():
        shutil.copy(LEGACY / "index.html", out.parent / "legacy.html")
    print(f"wrote {out.relative_to(ROOT)} ({len(page):,} bytes, env={env})")

if __name__ == "__main__":
    main()
