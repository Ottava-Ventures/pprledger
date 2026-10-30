#!/usr/bin/env python3
"""Pre-merge check for a static GitHub Pages site.

Fails the pull request when the site would publish with:
  - a link, image, font, icon or stylesheet pointing at a file that doesn't exist
  - a page linking to a #section that isn't on the target page
  - a sitemap.xml or site.webmanifest entry pointing at a missing file
  - a page with no <title>, or leftover placeholder text (lorem ipsum, TODO, FIXME)
  - a privacy page that is missing

Warns (without failing) about images that have no alt text.

Only files in this repository are checked; links to other websites are not
fetched, so the check never fails because some other site is slow or down.
Uses only the Python standard library.
"""
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
SKIP_DIRS = {".git", ".github", "node_modules"}
PLACEHOLDERS = re.compile(r"lorem ipsum|\bTODO\b|\bFIXME\b|\bTKTK\b|\[placeholder\]", re.I)
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")
LINK_ATTRS = {"href", "src", "poster", "data-src"}

errors, warnings = [], []


def site_domains():
    cname = ROOT / "CNAME"
    if not cname.exists():
        return set()
    host = cname.read_text().strip().lower()
    return {host, "www." + host} if host else set()


DOMAINS = site_domains()


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs = []          # (url, line)
        self.ids = set()
        self.has_title = False
        self.in_title = False
        self.title_text = ""
        self.in_style = False
        self.text = []
        self.missing_alt = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        line = self.getpos()[0]
        for key in ("id", "name"):
            if a.get(key):
                self.ids.add(a[key])
        for key in LINK_ATTRS:
            if a.get(key):
                # <a href> and <link href> and <img src> etc.
                if key == "href" and tag not in ("a", "link", "area"):
                    continue
                self.refs.append((a[key], line))
        for key in ("srcset", "imagesrcset"):
            if a.get(key):
                for part in a[key].split(","):
                    url = part.strip().split(" ")[0]
                    if url:
                        self.refs.append((url, line))
        if tag == "meta" and (a.get("property") in ("og:image", "og:url") or a.get("name") in ("twitter:image",)):
            if a.get("content"):
                self.refs.append((a["content"], line))
        if a.get("style"):
            for url in CSS_URL.findall(a["style"]):
                self.refs.append((url, line))
        if tag == "img" and "alt" not in a:
            self.missing_alt.append((a.get("src", "?"), line))
        if tag == "title":
            self.in_title = True
        if tag == "style":
            self.in_style = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
            self.has_title = bool(self.title_text.strip())
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_title:
            self.title_text += data
        if self.in_style:
            for url in CSS_URL.findall(data):
                self.refs.append((url, self.getpos()[0]))
        else:
            self.text.append(data)


def resolve(url, from_file):
    """Map a URL found in from_file to (path in repo, fragment), or None if external/ignorable."""
    url = url.strip()
    if not url or url.startswith(("mailto:", "tel:", "sms:", "data:", "javascript:", "blob:")):
        return None
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") or url.startswith("//"):
        if parts.netloc.lower() not in DOMAINS:
            return None  # another website: not checked
        path = parts.path or "/"
    elif parts.scheme:
        return None
    else:
        path = parts.path
    path = unquote(path)
    if path.startswith("/"):
        target = ROOT / path.lstrip("/")
    elif path == "":
        target = from_file
    else:
        target = from_file.parent / path
    target = Path(*target.parts)  # normalise ../
    try:
        target = target.resolve()
    except OSError:
        pass
    if target.is_dir() or path.endswith("/"):
        target = target / "index.html"
    elif not target.exists() and target.with_suffix(".html").exists() and not target.suffix:
        target = target.with_suffix(".html")  # GitHub Pages serves /about as about.html
    return target, parts.fragment


def rel(p):
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def main():
    pages = {}
    for f in sorted(ROOT.rglob("*.html")):
        if any(part in SKIP_DIRS for part in f.relative_to(ROOT).parts):
            continue
        p = Page()
        p.feed(f.read_text(encoding="utf-8", errors="replace"))
        pages[f.resolve()] = p

    if not pages:
        errors.append("No .html pages found.")

    for f, p in pages.items():
        name = rel(f)
        if not p.has_title:
            errors.append(f"{name}: page has no <title>")
        body = " ".join(p.text)
        m = PLACEHOLDERS.search(body)
        if m:
            errors.append(f"{name}: placeholder text left on the page: \"{m.group(0)}\"")
        for src, line in p.missing_alt:
            warnings.append(f"{name}:{line}: image {src} has no alt text (screen readers and search engines use it)")
        for url, line in p.refs:
            r = resolve(url, f)
            if url.startswith("#"):
                r = (f, url[1:])
            if not r:
                continue
            target, frag = r
            if not target.exists():
                errors.append(f"{name}:{line}: broken link to {url} (no file at {rel(target)})")
                continue
            if frag and target.suffix == ".html" and target in pages and frag not in pages[target].ids:
                errors.append(f"{name}:{line}: link to {url} points at a section (#{frag}) that isn't on that page")

    sitemap = ROOT / "sitemap.xml"
    if sitemap.exists():
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap.read_text()):
            r = resolve(loc, ROOT / "index.html")
            if r and not r[0].exists():
                errors.append(f"sitemap.xml: lists {loc}, but that page doesn't exist")
            if urlsplit(loc).netloc and DOMAINS and urlsplit(loc).netloc.lower() not in DOMAINS:
                warnings.append(f"sitemap.xml: {loc} is on a different domain than CNAME ({', '.join(sorted(DOMAINS))})")

    manifest = ROOT / "site.webmanifest"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text())
            for icon in data.get("icons", []):
                r = resolve(icon.get("src", ""), manifest)
                if r and not r[0].exists():
                    errors.append(f"site.webmanifest: icon {icon.get('src')} doesn't exist")
        except json.JSONDecodeError as e:
            errors.append(f"site.webmanifest: not valid JSON ({e})")

    if not (ROOT / "privacy" / "index.html").exists() and not (ROOT / "privacy.html").exists():
        errors.append("privacy page is missing (expected privacy/index.html)")

    for w in warnings:
        print(f"::warning::{w}")
    for e in errors:
        print(f"::error::{e}")
    print(f"\nChecked {len(pages)} page(s): {len(errors)} problem(s), {len(warnings)} warning(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
