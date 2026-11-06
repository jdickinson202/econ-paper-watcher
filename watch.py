"""Watch working-paper listing pages and publish what is new as one Atom feed.

  python watch.py                check every enabled source, update state/seen.json, write docs/feed.xml and docs/index.html
  python watch.py --check NAME   show what the recipe finds for each source whose name contains NAME ("all" for every
                                 source); writes nothing

Recipes live in sources.toml (the README explains the fields). The first time a source is checked, everything on its
page is recorded as already seen, so adding a source never floods the feed with its back catalogue.
"""
import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
import zlib
from concurrent.futures import ThreadPoolExecutor
from html import escape, unescape
from http.cookiejar import CookieJar
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCES = os.path.join(ROOT, "sources.toml")
STATE = os.path.join(ROOT, "state", "seen.json")
FEED = os.path.join(ROOT, "docs", "feed.xml")
STATUS = os.path.join(ROOT, "docs", "index.html")

BOT = "econ-paper-watcher"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
UA = f"Mozilla/5.0 (compatible; {BOT}/1.0; +https://github.com/{REPO})" if REPO else f"Mozilla/5.0 (compatible; {BOT}/1.0)"
TITLE = "New economics working papers: econ-paper-watcher"

FEED_ITEMS = 300         # entries kept in the feed
SEEN_PER_SOURCE = 2000   # links remembered per source
WARN_AFTER = 3           # failed runs in a row before the feed carries a warning
MAX_NEW = 25             # more new links than this at once become one item (a redesign, or a whole conference program)
GENERIC = re.compile(r"^\W*(pdf|download|read more|more|full text|view|details?|abstract|link|here|paper|open|"
                     r"access|english|español|\d+([.,]\d+)?\s*[km]b)\W*$", re.I)
TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|jsessionid|phpsessid|sessionid)$", re.I)


class RecipeError(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def load_sources(path=SOURCES):
    with open(path, "rb") as f:
        sources = tomllib.load(f)["source"]
    for s in sources:
        s.setdefault("enabled", True)
        s["id"] = s.get("id") or slug(f"{s['name']} {s.get('series', '')}")
    ids = [s["id"] for s in sources]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        sys.exit(f"sources.toml: two sources share an id: {dupes}")
    return sources


def page_url(src):
    return src["url"].format(year=now().year)


# --- fetching -------------------------------------------------------------------------------------------------------

_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
_robots = {}


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    with _opener.open(req, timeout=timeout) as r:
        body = r.read(15_000_000)
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            body = gzip.decompress(body)
        elif enc == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
        return r.geturl(), body, r.headers.get_content_charset()


def robots_allow(url):
    parts = urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            _, body, charset = fetch(root + "/robots.txt", timeout=20)
            try:
                text = body.decode(charset or "utf-8", "replace")
            except LookupError:  # a charset Python doesn't know, such as the "iso-8852" banque-france.fr declares
                text = body.decode("utf-8", "replace")
            rp.parse(text.splitlines())
        except urllib.error.HTTPError as e:
            # RFC 9309: a 4xx answer means there are no rules; a 5xx (or 429) means the site can't say right now
            if e.code >= 500 or e.code == 429:
                rp = None
            else:
                rp.allow_all = True
        except Exception:
            rp = None
        _robots[root] = rp
    if _robots[root] is None:
        raise RecipeError("the site's robots.txt could not be read, so the watcher stays away until a later run")
    return _robots[root].can_fetch(BOT, url)


def describe(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"the site answered HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"could not connect ({e.reason})"
    if isinstance(e, (RecipeError, TimeoutError)):
        return str(e) or "timed out"
    return f"{type(e).__name__}: {e}"[:200]


# --- extracting papers ----------------------------------------------------------------------------------------------

def text_of(el):
    text = re.sub(r"[​-‏⁠﻿]", "", el.get_text(" ", strip=True))  # zero-width marks
    return re.sub(r"\s+", " ", text).strip()


def nearby_title(a, min_len):
    """For links whose text is just "PDF" or "Read more": the closest heading or bold text around the link."""
    node = a
    for _ in range(4):
        node = node.parent
        if node is None:
            break
        for cand in node.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"]) + node.select('[class*="title"]'):
            t = text_of(cand)
            if len(t) >= min_len and not GENERIC.match(t):
                return t
    return ""


def clean_url(url, strip_query=False):
    p = urlsplit(url)
    query = p.query
    if strip_query:
        query = ""
    elif query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(TRACKING.match(k) for k, _ in pairs):
            query = urlencode([(k, v) for k, v in pairs if not TRACKING.match(k)])
    path = re.sub(r";jsessionid=[^/?#]*", "", p.path, flags=re.I)
    # percent-encode spaces and other characters a URI may not contain, leaving existing %XX escapes alone
    path = quote(path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(query, safe="/?%:@!$&'()*+,;=-._~")
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, query, ""))


def resolve(src, base, link):
    """The absolute, cleaned form of a link, after the recipe's rewrite rules ([pattern, replacement] pairs)."""
    for pattern, repl in src.get("rewrite", []):
        link = re.sub(pattern, repl, link)
    return clean_url(urljoin(base, link.strip()), src.get("strip_query", False))


def extract(src, base, body, charset):
    """{paper url: title} for the links on one listing page that the recipe accepts."""
    soup = BeautifulSoup(body, "html.parser", from_encoding=charset)
    for junk in soup(["script", "style", "noscript", "template"]):
        junk.decompose()
    scopes = soup.select(src["scope"]) if src.get("scope") else [soup]
    if not scopes:
        raise RecipeError(f"nothing on the page matches scope {src['scope']!r}; its layout may have changed")
    want = re.compile(src["links"], re.I) if src.get("links") else None
    skip = re.compile(src["skip"], re.I) if src.get("skip") else None
    min_len = src.get("min_title", 12)
    found = {}
    for scope in scopes:
        for node in scope.select(src["item"]) if src.get("item") else [scope]:
            anchors = node.find_all("a", href=True)
            if node.name == "a" and node.get("href"):
                anchors.insert(0, node)
            for a in anchors:
                href = a["href"].strip()
                if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                    continue
                url = resolve(src, base, href)
                if (want and not want.search(url)) or (skip and skip.search(url)):
                    continue
                if src.get("title"):
                    t = node.select_one(src["title"])
                    title = text_of(t) if t else ""
                elif src.get("item_text"):
                    title = text_of(node)
                else:
                    title = text_of(a) or a.get("title", "").strip() or a.get("aria-label", "").strip()
                    if len(title) < min_len or GENERIC.match(title):
                        title = nearby_title(a, min_len) or title
                if src.get("title_match"):  # keep only the regex's first group, when it matches
                    m = re.search(src["title_match"], title)
                    title = m.group(1).strip() if m else title
                if len(title) < min_len or GENERIC.match(title):
                    continue
                if len(title) > len(found.get(url, "")):
                    found[url] = title[:300]
                if src.get("item"):
                    break  # one paper per item
    return found


def keep(src, url, title, min_len, found):
    """Add one candidate paper to found if the recipe's link filters and title rules accept it."""
    if (src.get("links") and not re.search(src["links"], url, re.I)) or (src.get("skip") and re.search(src["skip"], url, re.I)):
        return
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) >= min_len and not GENERIC.match(title) and len(title) > len(found.get(url, "")):
        found[url] = title[:300]


def tag_text(s, tag):
    m = re.search(r"<(?:\w+:)?%s\b[^>]*(?<!/)>(.*?)</(?:\w+:)?%s>" % (tag, tag), s, re.I | re.S)
    if not m:
        return ""
    v = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)
    return unescape(re.sub(r"<[^>]+>", " ", v)).strip()


def extract_feed(src, base, body):
    """kind = "feed": the items of an RSS or Atom feed (read with regular expressions, so no XML entities expand)."""
    text = body.decode("utf-8", "replace")
    found = {}
    for m in re.finditer(r"<((?:\w+:)?(?:item|entry))\b.*?</\1>", text, re.S | re.I):
        it = m.group(0)
        link = tag_text(it, "link")
        if not link:
            href = re.search(r"<(?:\w+:)?link\b[^>]*href=[\"']([^\"']+)", it, re.I)
            link = unescape(href.group(1)) if href else tag_text(it, "guid")
        if link:
            keep(src, resolve(src, base, link), tag_text(it, "title"), src.get("min_title", 10), found)
    return found


def pick(obj, path):
    """A field of parsed JSON: the key itself when present (some APIs use keys like "dc.title"), else a dotted path."""
    if not path:
        return obj
    if isinstance(obj, dict) and path in obj:
        return obj[path]
    for key in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(key)] if key.isdigit() and int(key) < len(obj) else ""
        elif isinstance(obj, dict):
            obj = obj.get(key, "")
        else:
            return ""
    return obj


def extract_json(src, base, body):
    """kind = "json": a list inside a JSON response. json_items is the list's dotted path, json_title the title
    field and json_link either the link field or a template such as "https://example.org/paper?id={id}"."""
    data = pick(json.loads(body.decode("utf-8-sig", "replace")), src.get("json_items", ""))
    found = {}
    for it in data if isinstance(data, list) else []:
        tmpl = src["json_link"]
        link = tmpl.format(**it) if "{" in tmpl else str(pick(it, tmpl))
        title = unescape(re.sub(r"<[^>]+>", " ", str(pick(it, src.get("json_title", "title")))))
        if link:
            keep(src, resolve(src, base, link), title, src.get("min_title", 10), found)
    return found


def extract_regex(src, base, body, charset):
    """kind = "regex": title and link pairs in the page's raw text, for a list kept as data in a script on the page.
    pattern is a regular expression with groups named title and link."""
    try:
        text = body.decode(charset or "utf-8", "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    found = {}
    for m in re.finditer(src["pattern"], text):
        title = unescape(m["title"])
        if src.get("title_match"):
            kept = re.search(src["title_match"], title)
            title = kept.group(1).strip() if kept else title
        keep(src, resolve(src, base, unescape(m["link"])), title, src.get("min_title", 10), found)
    return found


def check(src):
    url = page_url(src)
    if not robots_allow(url):
        raise RecipeError("the site's robots.txt asks bots to stay out of this page")
    final, body, charset = fetch(url)
    kind = src.get("kind", "html")
    if kind == "feed":
        return extract_feed(src, final, body)
    if kind == "json":
        return extract_json(src, final, body)
    if kind == "regex":
        return extract_regex(src, final, body, charset)
    return extract(src, final, body, charset)


# --- state and output -----------------------------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"sources": {}, "items": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def attempt(src):
    """(src, papers found, error) for one source; never raises, so one broken source can't stop the others."""
    try:
        found = check(src)
        if not found and not src.get("allow_empty"):
            raise RecipeError("the recipe found no papers on the page; its layout may have changed")
        return src, found, None
    except Exception as e:
        return src, None, e


def run(sources, state, stamp):
    """Check every enabled source once, recording new papers and failures in state."""
    new_items = []
    # a few sources at a time; each site still gets just its robots.txt and one page request per series per run
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, [s for s in sources if s["enabled"]]))
    for src, found, e in results:
        st = state["sources"].setdefault(src["id"], {})
        st["last_try"] = stamp
        if e is not None:
            if not st.get("fails"):
                st["fail_since"] = stamp
            st["fails"] = st.get("fails", 0) + 1
            st["last_error"] = describe(e)
            print(f"!! {src['name']}: {st['last_error']}")
            if st["fails"] >= WARN_AFTER and not st.get("warned"):
                st["warned"] = stamp
                new_items.append(dict(
                    id=f"urn:{BOT}:warning:{src['id']}:{stamp[:10]}", kind="warning", name=TITLE, series="",
                    title=f"{src['name']} has failed {st['fails']} runs in a row: {st['last_error']}",
                    summary="Its recipe in sources.toml probably needs fixing; see the README.",
                    url=page_url(src), first_seen=stamp))
            continue
        seen = st.setdefault("seen", {})
        fresh = [(u, t) for u, t in found.items() if u not in seen]
        for u in found:
            seen.setdefault(u, stamp)
        if "first_ok" in st and fresh:  # a source's first successful check only records what is already there
            st["last_new"] = stamp
            if len(fresh) > src.get("max_new", MAX_NEW):
                new_items.append(dict(
                    id=f"urn:{BOT}:bulk:{src['id']}:{stamp[:10]}", kind="bulk", name=src["name"],
                    series=src.get("series", ""), title=f"{src['name']}: {len(fresh)} new links at once",
                    summary="Either many papers went up together or the site changed its links. The first few: "
                            + "; ".join(t for _, t in fresh[:5]),
                    url=page_url(src), first_seen=stamp))
            else:
                new_items += [dict(id=u, name=src["name"], series=src.get("series", ""), title=t, url=u,
                                   first_seen=stamp) for u, t in fresh]
        print(f"ok {src['name']}: {len(found)} on the page, "
              + (f"{len(fresh)} new" if "first_ok" in st else "first check, all recorded as seen"))
        st.update(first_ok=st.get("first_ok", stamp), last_ok=stamp, last_count=len(found), fails=0,
                  last_error="", warned="", fail_since="")
        if len(seen) > SEEN_PER_SOURCE:
            for u in sorted(seen, key=seen.get)[:len(seen) - SEEN_PER_SOURCE]:
                del seen[u]
    ids = {s["id"] for s in sources}
    state["sources"] = {k: v for k, v in state["sources"].items() if k in ids}
    state["items"] = (new_items + state["items"])[:FEED_ITEMS]


def feed_url():
    if not REPO:
        return ""
    owner, name = REPO.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/feed.xml"


def write_feed(state, stamp):
    entries, ids = [], set()
    for it in state["items"]:
        if it["id"] in ids:
            continue
        ids.add(it["id"])
        title = it["title"] if it.get("kind") else f"{it['name']}: {it['title']}"
        summary = it.get("summary") or (f"{it['series']}. " if it["series"] else "") + f"First seen {it['first_seen'][:10]}."
        entries.append(
            "  <entry>\n"
            f"    <title>{escape(title)}</title>\n"
            f"    <link href=\"{escape(it['url'])}\"/>\n"
            f"    <id>{escape(it['id'])}</id>\n"
            f"    <updated>{it['first_seen']}</updated>\n"
            f"    <author><name>{escape(it['name'])}</name></author>\n"
            f"    <summary>{escape(summary)}</summary>\n"
            "  </entry>")
    self_link = f"  <link rel=\"self\" href=\"{escape(feed_url())}\"/>\n" if feed_url() else ""
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n'
           f"  <title>{TITLE}</title>\n"
           "  <subtitle>New papers from economics working-paper series that are hard to follow any other way"
           "</subtitle>\n"
           f"  <id>urn:{BOT}:{escape(REPO or 'local')}</id>\n{self_link}"
           f"  <updated>{stamp}</updated>\n" + "\n".join(entries) + "\n</feed>\n")
    os.makedirs(os.path.dirname(FEED), exist_ok=True)
    with open(FEED, "w", encoding="utf-8") as f:
        f.write(xml)


def write_status(sources, state, stamp):
    rows = []
    counts = {"ok": 0, "bad": 0, "off": 0}
    for src in sorted(sources, key=lambda s: s["name"].lower()):
        st = state["sources"].get(src["id"], {})
        if not src["enabled"]:
            cls = "off"
            status = "Not watched: " + escape(src.get("reason", "turned off in sources.toml"))
            if src.get("alert"):
                status += f' <a href="{escape(src["alert"])}">Email alerts</a>'
        elif st.get("fails"):
            cls = "bad"
            status = f"Failing since {st['fail_since'][:10]}: {escape(st['last_error'])}"
        elif st.get("last_ok"):
            cls = "ok"
            status = f"Watching ({st['last_count']} papers on the page)"
        else:
            cls = "off"
            status = "Not checked yet"
        counts[cls] += 1
        rows.append(f"<tr class=\"{cls}\"><td><a href=\"{escape(page_url(src))}\">{escape(src['name'])}</a>"
                    f"<div class=\"series\">{escape(src.get('series', ''))}</div></td><td>{status}</td>"
                    f"<td>{(st.get('last_new') or '')[:10] or '–'}</td></tr>")
    recent = "".join(
        f"<li><span class=\"date\">{it['first_seen'][:10]}</span> <a href=\"{escape(it['url'])}\">"
        f"{escape(it['title'])}</a> <span class=\"src\">{escape(it['name'])}</span></li>"
        for it in [i for i in state["items"] if not i.get("kind")][:60])
    feed = feed_url() or "feed.xml"
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{TITLE}</title>
<link rel="alternate" type="application/atom+xml" title="{TITLE}" href="feed.xml">
<style>
  :root {{ color-scheme: light dark; --fg: #1d2330; --muted: #5d6675; --line: #d9dde3; --bg: #ffffff;
          --ok: #1f7a43; --bad: #b3261e; --off: #7a7f87; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg: #e6e8ec; --muted: #a2a9b4; --line: #343a45; --bg: #15181d; --ok: #6fcf97; --bad: #ff8a80; --off: #9aa0a8; }}
  }}
  body {{ margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; }}
  main {{ max-width: 980px; margin: 0 auto; padding: 24px 16px 48px; }}
  h1 {{ font-size: 24px; margin: 0 0 4px; }}
  h2 {{ font-size: 18px; margin: 32px 0 8px; }}
  p {{ margin: 4px 0; color: var(--muted); }}
  a {{ color: inherit; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; vertical-align: top; padding: 8px 6px; border-bottom: 1px solid var(--line); }}
  th {{ font-size: 13px; color: var(--muted); font-weight: 600; }}
  .series, .src, .date {{ color: var(--muted); font-size: 13px; }}
  tr.ok td:nth-child(2) {{ color: var(--ok); }}
  tr.bad td:nth-child(2) {{ color: var(--bad); }}
  tr.off td:nth-child(2) {{ color: var(--off); }}
  ul {{ padding-left: 18px; }}
  li {{ margin: 4px 0; }}
  .feed {{ display: inline-block; margin-top: 8px; padding: 6px 12px; border: 1px solid var(--line); border-radius: 6px;
           text-decoration: none; font-weight: 600; }}
</style>
</head>
<body>
<main>
<h1>{TITLE}</h1>
<p>Checked once a day: {counts['ok']} sources watched, {counts['bad']} failing, {counts['off']} not watched.
Last run {stamp[:16].replace('T', ' ')} UTC.</p>
<a class="feed" href="feed.xml">Subscribe: {escape(feed)}</a>
<h2>Recently found</h2>
<ul>{recent or '<li>Nothing new yet. The first run only records what is already on each page.</li>'}</ul>
<h2>Sources</h2>
<table>
<tr><th>Source</th><th>Status</th><th>Last new paper</th></tr>
{''.join(rows)}
</table>
</main>
</body>
</html>
"""
    with open(STATUS, "w", encoding="utf-8") as f:
        f.write(page)


def show(sources, pattern):
    picked = [s for s in sources if pattern == "all" or pattern.lower() in s["name"].lower()]
    if not picked:
        sys.exit(f"no source name contains {pattern!r}")
    for src in picked:
        if not src["enabled"]:
            print(f"-- {src['name']}: not watched ({src.get('reason', 'turned off')})")
            continue
        try:
            found = check(src)
        except Exception as e:
            print(f"!! {src['name']}: {describe(e)}")
            continue
        print(f"{'ok' if found else '!!'} {src['name']}: {len(found)} papers on the page")
        for u, t in list(found.items())[:4]:
            print(f"     {t[:100]}\n       {u}")
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", metavar="NAME", help="show what the recipes find; writes nothing")
    ap.add_argument("--sources", default=SOURCES, help="recipe file to use with --check (default: sources.toml)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    sources = load_sources(args.sources if args.check else SOURCES)
    if args.check:
        show(sources, args.check)
        return
    stamp = iso(now())
    state = load_state()
    run(sources, state, stamp)
    state["last_run"] = stamp
    save_state(state)
    write_feed(state, stamp)
    write_status(sources, state, stamp)


if __name__ == "__main__":
    main()
