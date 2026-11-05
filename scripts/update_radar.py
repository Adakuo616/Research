#!/usr/bin/env python3
"""Daily update for the research radar.

Fetches new articles for every journal in config/sources.json from the
Crossref API, tags them by keyword, optionally writes Traditional Chinese
summaries with the Claude API, refreshes scholar metrics and recent works
from OpenAlex, and writes docs/data/radar.json plus docs/data/radar-data.js
(the page loads the .js file so it also works when opened from disk).

Usage:
    python scripts/update_radar.py            # normal run
    python scripts/update_radar.py --no-llm   # skip Claude summaries
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "sources.json")
OUT_DIR = os.path.join(ROOT, "docs", "data")
OUT_JSON = os.path.join(OUT_DIR, "radar.json")
OUT_JS = os.path.join(OUT_DIR, "radar-data.js")
UA = "research-radar/1.0 (+https://github.com/Adakuo616/Research)"
TAIPEI = dt.timezone(dt.timedelta(hours=8))
SKIP_TITLES = re.compile(r"^(editorial board|erratum|corrigendum|retraction|cover|table of contents|issue information|front matter|back matter|publisher'?s note|announcement)", re.I)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def openalex_url(url):
    """Add the optional OpenAlex API key (repo secret OPENALEX_API_KEY)."""
    key = os.environ.get("OPENALEX_API_KEY")
    return url + ("&" if "?" in url else "?") + "api_key=" + urllib.parse.quote(key) if key else url


def get_json(url, tries=5):
    for i in range(tries):
        wait = 2 ** i
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                try:
                    wait = max(wait, min(60, int(e.headers.get("Retry-After", "5"))))
                except ValueError:
                    wait = max(wait, 5)
            log(f"  HTTP {e.code} for {url.split('?')[0]} (try {i + 1}, waiting {wait}s)")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            log(f"  {type(e).__name__} for {url.split('?')[0]} (try {i + 1})")
        time.sleep(wait)
    return None


def clean_abstract(raw):
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return re.sub(r"^(abstract|summary)\s*[:.]?\s*", "", text, flags=re.I)


def date_of(item, key):
    parts = (item.get(key) or {}).get("date-parts") or [[]]
    p = parts[0] + [1, 1, 1]
    if not p[0]:
        return ""
    return f"{p[0]:04d}-{p[1]:02d}-{p[2]:02d}"


def short_authors(item, limit=6):
    names = []
    for a in item.get("author", [])[:limit]:
        n = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
        if n:
            names.append(n)
    if len(item.get("author", [])) > limit:
        names.append("et al.")
    return ", ".join(names) or "—"


def tag_text(text, tags):
    t = text.lower()
    return [tg["id"] for tg in tags if any(term.lower() in t for term in tg["terms"])]


CR_SELECT = "DOI,title,author,container-title,created,published,abstract,URL,type"


def crossref_journal(j, since):
    items = []
    if j.get("issn"):
        for issn in j["issn"]:
            url = (f"https://api.crossref.org/journals/{issn}/works?rows=200&select={CR_SELECT}"
                   f"&filter=from-created-date:{since}&sort=created&order=desc")
            data = get_json(url)
            if data is not None:
                items = data["message"]["items"]
                break
    elif j.get("query"):
        q = urllib.parse.quote(j["query"])
        url = (f"https://api.crossref.org/works?rows=200&select={CR_SELECT}"
               f"&query.container-title={q}&filter=from-created-date:{since}")
        data = get_json(url) or {"message": {"items": []}}
        must = (j.get("must_contain") or "").lower()
        items = [it for it in data["message"]["items"]
                 if must in " ".join(it.get("container-title", [])).lower()]
    return items


def fetch_articles(cfg, since, today):
    out = []
    for j in cfg["journals"]:
        raw = crossref_journal(j, since)
        kept = 0
        for it in raw:
            title = html.unescape(re.sub(r"<[^>]+>", "", " ".join(it.get("title") or []))).strip()
            if not title or SKIP_TITLES.match(title):
                continue
            abstract = clean_abstract(it.get("abstract"))
            tags = tag_text(title + " " + abstract, cfg["tags"])
            if cfg.get("require_tag", True) and not tags:
                continue
            doi = it.get("DOI", "").lower()
            out.append({
                "doi": doi,
                "url": f"https://doi.org/{doi}" if doi else it.get("URL", ""),
                "j": j["id"],
                "journal": j["name"],
                "title": title,
                "authors": short_authors(it),
                "online": date_of(it, "created"),
                "published": date_of(it, "published"),
                "abstract": abstract[:1200],
                "tags": tags,
                "zh": "",
                "en": "",
                "added": today,
            })
            kept += 1
        log(f"{j['id']:7s} fetched {len(raw):4d}, kept {kept}")
        time.sleep(0.4)
    return out


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "doi": {"type": "string"},
                    "zh": {"type": "string"},
                    "en": {"type": "string"},
                },
                "required": ["doi", "zh", "en"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SUMMARY_PROMPT = """You write short bilingual digests for a PhD student in industrial engineering who studies agent-based modelling, reliability of stochastic-flow networks, simulation and project management.

For each article below, return:
- zh: one or two sentences in Traditional Chinese (繁體中文) stating what the paper does and its method or finding. Use plain academic wording; keep technical terms in English in parentheses when helpful.
- en: one sentence in English with the paper's core contribution, based on the abstract when present, otherwise on the title.
Only use information in the title and abstract. If there is no abstract, say what the title indicates without inventing results.
Return one entry per article, with the doi copied exactly.

Articles:
"""


def summarize(items, cfg):
    """Fill item['zh'] / item['en'] using the Claude API. Returns count summarised."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("ANTHROPIC_API_KEY not set: skipping summaries")
        return 0
    try:
        import anthropic
    except ImportError:
        log("anthropic package not installed: skipping summaries")
        return 0
    s = cfg.get("summarize", {})
    model = os.environ.get("RADAR_MODEL") or s.get("model", "claude-opus-5-5")
    todo = [it for it in items if not it.get("zh")][: int(s.get("max_per_run", 60))]
    client = anthropic.Anthropic()
    done = 0
    for start in range(0, len(todo), 10):
        batch = todo[start:start + 10]
        body = "\n\n".join(
            f"doi: {it['doi']}\ntitle: {it['title']}\njournal: {it['journal']}\nabstract: {it['abstract'] or '(none)'}"
            for it in batch)
        try:
            resp = client.beta.messages.create(
                model=model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
                messages=[{"role": "user", "content": SUMMARY_PROMPT + body}],
            )
        except anthropic.AuthenticationError:
            log("Claude API key rejected: skipping summaries")
            return done
        except anthropic.RateLimitError:
            log("Claude API rate limited: stopping summaries for this run")
            return done
        except anthropic.APIStatusError as e:
            log(f"Claude API error {e.status_code}: skipping this batch")
            continue
        except anthropic.APIConnectionError:
            log("Claude API connection error: skipping this batch")
            continue
        if resp.stop_reason == "refusal":
            log("Claude declined this batch: left without summary")
            continue
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            rows = json.loads(text)["items"]
        except (json.JSONDecodeError, KeyError):
            log("Could not parse summary JSON for a batch")
            continue
        by_doi = {r["doi"].lower(): r for r in rows}
        for it in batch:
            r = by_doi.get(it["doi"])
            if r:
                it["zh"], it["en"] = r["zh"].strip(), r["en"].strip()
                done += 1
    return done


def backfill_abstracts(items):
    """Crossref often lacks abstracts (e.g. Elsevier); OpenAlex usually has them."""
    need = [a for a in items if not a.get("abstract") and a.get("doi")]
    filled = 0
    for start in range(0, len(need), 40):
        chunk = need[start:start + 40]
        ids = "|".join("https://doi.org/" + a["doi"] for a in chunk)
        url = (f"https://api.openalex.org/works?filter=doi:{urllib.parse.quote(ids, safe=':/|')}"
               f"&per-page=50&select=doi,abstract_inverted_index")
        data = get_json(openalex_url(url)) or {"results": []}
        by_doi = {}
        for w in data["results"]:
            inv = w.get("abstract_inverted_index") or {}
            if not inv or not w.get("doi"):
                continue
            pos = sorted((i, word) for word, idx in inv.items() for i in idx)
            by_doi[w["doi"].lower().replace("https://doi.org/", "")] = " ".join(word for _, word in pos)
        for a in chunk:
            if a["doi"] in by_doi:
                a["abstract"] = clean_abstract(by_doi[a["doi"]])[:1200]
                filled += 1
        time.sleep(1.5)
    log(f"abstracts filled from OpenAlex: {filled}/{len(need)}")


def initials_name(display):
    parts = display.replace("‐", "-").split()
    if len(parts) < 2:
        return display
    given = "".join(p[0].upper() for g in parts[:-1] for p in g.split("-") if p)
    return f"{given} {parts[-1]}"


def fetch_scholars(cfg, prev, today):
    terms = [t.lower() for t in cfg.get("scholar_topic_terms", [])]
    year_from = dt.date.today().year - 1
    result = {}
    for sc in cfg["scholars"]:
        old = {w["title"].lower(): w for w in (prev.get(sc["key"], {}).get("works") or [])}
        stats = None
        works = {}
        for aid in sc["openalex"]:
            a = get_json(openalex_url(f"https://api.openalex.org/authors/{aid}"))
            if a and stats is None:
                ss = a.get("summary_stats") or {}
                stats = {"cites": a.get("cited_by_count"), "h": ss.get("h_index"), "i10": ss.get("i10_index"),
                         "works": a.get("works_count")}
            url = (f"https://api.openalex.org/works?filter=author.id:{aid},from_publication_date:{year_from}-01-01"
                   f"&sort=publication_date:desc&per-page=50"
                   f"&select=doi,title,publication_year,publication_date,authorships,primary_location,cited_by_count")
            data = get_json(openalex_url(url)) or {"results": []}
            for w in data["results"]:
                title = re.sub(r"<[^>]+>", "", w.get("title") or "").strip()
                if not title or (terms and not any(t in title.lower() for t in terms)):
                    continue
                key = title.lower()
                src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "—"
                authors = ", ".join(initials_name(x["author"]["display_name"]) for x in w.get("authorships", [])[:6])
                works[key] = {
                    "year": w.get("publication_year"),
                    "date": w.get("publication_date"),
                    "title": title,
                    "authors": authors,
                    "venue": src,
                    "cites": w.get("cited_by_count") or 0,
                    "doi": w.get("doi"),
                    "first_seen": old.get(key, {}).get("first_seen", today),
                }
            time.sleep(1.5)
        result[sc["key"]] = {"name": sc["name"], "stats": stats or {},
                             "works": sorted(works.values(), key=lambda x: x["date"] or "", reverse=True)}
        log(f"scholar {sc['key']}: {len(works)} recent works, stats {stats}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="skip Claude summaries")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    os.makedirs(OUT_DIR, exist_ok=True)
    prev = json.load(open(OUT_JSON, encoding="utf-8")) if os.path.exists(OUT_JSON) else {}
    now = dt.datetime.now(TAIPEI)
    today = now.date().isoformat()
    since = (now.date() - dt.timedelta(days=int(cfg.get("lookback_days", 7)))).isoformat()

    old_feed = {a["doi"]: a for a in prev.get("feed", []) if a.get("doi")}
    fresh = fetch_articles(cfg, since, today)
    new_items = []
    for a in fresh:
        if a["doi"] in old_feed:
            o = old_feed[a["doi"]]
            o["tags"] = a["tags"]  # re-tag in case keywords changed
            if not o.get("abstract") and a["abstract"]:
                o["abstract"] = a["abstract"]
        else:
            old_feed[a["doi"]] = a
            new_items.append(a)

    backfill_abstracts([a for a in old_feed.values() if a.get("added") == today])
    for a in old_feed.values():
        if a.get("added") == today:
            a["tags"] = tag_text(a["title"] + " " + a.get("abstract", ""), cfg["tags"]) or a["tags"]

    cutoff = (now.date() - dt.timedelta(days=int(cfg.get("keep_days", 120)))).isoformat()
    feed = [a for a in old_feed.values() if a.get("added", today) >= cutoff]
    feed.sort(key=lambda a: (a.get("added", ""), a.get("online", "")), reverse=True)

    n_sum = 0
    if not args.no_llm and cfg.get("summarize", {}).get("enabled", True):
        n_sum = summarize(feed, cfg)

    scholars = fetch_scholars(cfg, prev.get("scholars", {}), today)

    out = {
        "meta": {"updated": now.isoformat(timespec="minutes"), "since": since,
                 "new_today": len(new_items), "summarised": n_sum, "total": len(feed)},
        "sources": {"tags": cfg["tags"], "journals": [{"id": j["id"], "name": j["name"]} for j in cfg["journals"]],
                    "scholars": [{"key": s["key"], "name": s["name"]} for s in cfg["scholars"]]},
        "feed": feed,
        "scholars": scholars,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("window.RADAR_DATA = " + json.dumps(out, ensure_ascii=False) + ";\n")
    log(f"done: {len(new_items)} new articles, {n_sum} summarised, {len(feed)} in feed")


if __name__ == "__main__":
    main()
