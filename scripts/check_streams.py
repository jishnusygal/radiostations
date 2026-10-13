#!/usr/bin/env python3
"""Validate every stream in stations.json and repair broken ones.

For each station whose streamUrl fails validation (after retries) we try, in order:
  1. the station's optional "fallbackUrls" list
  2. a search on the community Radio Browser directory (https://www.radio-browser.info)
A replacement is only accepted if it passes the same playback validation.

Writes stations.json in place (unless --dry-run) and a Markdown report to --report.
Exit code is always 0 on a normal run; "unresolved" stations are listed in the report.
Stdlib only.
"""
import argparse
import difflib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; OurRadioStreamChecker/1.0)"
TIMEOUT = 15
RETRIES = 3
RETRY_DELAY = 5
RB_SERVERS = ["de1.api.radio-browser.info", "nl1.api.radio-browser.info", "at1.api.radio-browser.info"]


def fetch(url, max_bytes=4096, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Icy-MetaData": "0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, (r.headers.get("Content-Type") or "").lower(), r.read(max_bytes), r.geturl()


def check_once(url, depth=0):
    """Return (ok, reason)."""
    try:
        status, ctype, body, final = fetch(url)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # DNS, TLS, timeout, reset...
        return False, f"{type(e).__name__}: {e}"
    if status != 200:
        return False, f"HTTP {status}"
    text = body.decode("utf-8", "ignore")
    is_playlist = text.lstrip().startswith("#EXTM3U") or "mpegurl" in ctype
    if is_playlist:
        if depth >= 2:
            return True, "playlist"
        # HLS: make sure the first variant / segment really loads
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        if not lines:
            return False, "empty playlist"
        return check_once(urllib.parse.urljoin(final, lines[0]), depth + 1)
    if "text/html" in ctype or text.lstrip().lower().startswith(("<!doctype", "<html")):
        return False, "got a web page, not audio"
    if ctype.startswith(("audio/", "application/ogg", "application/octet-stream", "video/mp2t", "video/")) or not ctype or "icy" in ctype:
        return (len(body) > 0), ("ok" if body else "empty body")
    return False, f"unexpected content-type {ctype}"


def is_playable(url, retries=RETRIES):
    reason = ""
    for i in range(retries):
        ok, reason = check_once(url)
        if ok:
            return True, reason
        if i < retries - 1:
            time.sleep(RETRY_DELAY)
    return False, reason


def image_ok(url):
    if not url:
        return True
    try:
        status, ctype, _, _ = fetch(url, max_bytes=16)
        return status == 200 and ctype.startswith("image/")
    except Exception:
        return False


def rb_search(query, tries=2):
    qs = urllib.parse.urlencode({
        "name": query, "countrycode": "IN", "hidebroken": "true",
        "order": "clickcount", "reverse": "true", "limit": 25,
    })
    for host in RB_SERVERS:
        try:
            _, _, body, _ = fetch(f"https://{host}/json/stations/search?{qs}", max_bytes=2_000_000)
            return json.loads(body)
        except Exception:
            continue
    return []


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def find_replacement(st, tried):
    query = st.get("searchName") or st["name"]
    cands = []
    for c in rb_search(query):
        score = difflib.SequenceMatcher(None, norm(query), norm(c.get("name", ""))).ratio()
        if score < 0.6 or not c.get("lastcheckok"):
            continue
        url = c.get("url_resolved") or c.get("url")
        if url and url not in tried:
            cands.append((score, c.get("clickcount", 0), url, c))
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    for score, _, url, c in cands[:8]:
        ok, _ = is_playable(url, retries=1)
        if ok:
            return url, c
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="stations.json")
    ap.add_argument("--report", default="report.md")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as f:
        stations = json.load(f)

    broken, rows = [], []
    for st in stations:
        ok, reason = is_playable(st["streamUrl"])
        print(f"[{'OK ' if ok else 'BAD'}] {st['name']}: {reason}")
        if not ok:
            broken.append((st, reason))

    # If most things fail, the runner's network is probably the problem. Don't touch anything.
    if stations and len(broken) > len(stations) / 2:
        msg = f"{len(broken)}/{len(stations)} streams failed - assuming a network problem, no changes made."
        print(msg)
        open(args.report, "w").write(f"## Stream check aborted\n\n{msg}\n")
        return 0

    changed, unresolved = [], []
    for st, reason in broken:
        old = st["streamUrl"]
        tried = {old}
        new, source = None, None
        for u in st.get("fallbackUrls", []):
            tried.add(u)
            if is_playable(u, retries=2)[0]:
                new, source = u, "fallbackUrls"
                break
        if not new:
            new, c = find_replacement(st, tried)
            if new:
                source = f"Radio Browser ({c.get('name')})"
        if new:
            st["streamUrl"] = new
            changed.append(f"- **{st['name']}**: `{old}` ({reason}) -> `{new}` via {source}")
        else:
            unresolved.append(f"- **{st['name']}**: `{old}` ({reason})")

    bad_images = [st["name"] for st in stations if not image_ok(st.get("imageUrl", ""))]

    out = ["## Stream check"]
    out.append(f"Checked {len(stations)} stations.\n")
    if changed:
        out += ["### Fixed automatically"] + changed + [""]
    if unresolved:
        out += ["### Still broken (needs a manual fix)"] + unresolved + [""]
    if bad_images:
        out += ["### Broken logo URLs"] + [f"- {n}" for n in bad_images] + [""]
    if not (changed or unresolved or bad_images):
        out.append("All streams and logos are healthy.")
    open(args.report, "w").write("\n".join(out) + "\n")
    print("\n".join(out))

    if changed and not args.dry_run:
        with open(args.file, "w", encoding="utf-8") as f:
            json.dump(stations, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
