#!/usr/bin/env python3
"""Write 12-section bilingual deep analyses for every tracked paper.

Sources, in priority order: the daily article feed, the topic libraries,
and every tracked scholar's publications (the curated lists in
docs/index.html plus the Crossref lists in docs/data/radar.json).
Papers that already have an analysis are skipped. Each run analyses at most
`deep.max_per_run` papers, so a large backlog is worked through over
several days.

Abstracts come from the feed, then Crossref, OpenAlex (with
OPENALEX_API_KEY) and Semantic Scholar. When no abstract can be found the
analysis is written from the title alone and marked as such.

Output: docs/data/deep.json and docs/data/deep-data.js (window.RADAR_DEEP).
Requires ANTHROPIC_API_KEY; without it the script exits without changes.
"""
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_radar as ur  # noqa: E402  (shared HTTP helpers)

ROOT = ur.ROOT
OUT_JSON = os.path.join(ur.OUT_DIR, "deep.json")
OUT_JS = os.path.join(ur.OUT_DIR, "deep-data.js")
PAGE = os.path.join(ROOT, "docs", "index.html")

FIELDS = ["background", "research_question", "theory", "constructs", "construct_def", "variables",
          "model", "method", "findings", "contributions", "limitations", "extended_ideas"]

SCHEMA = {
    "type": "object",
    "properties": dict(
        {"title_zh": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}}},
        **{f"{f}_{lang}": {"type": "string"} for f in FIELDS for lang in ("zh", "en")}),
    "required": ["title_zh", "keywords"] + [f"{f}_{lang}" for f in FIELDS for lang in ("zh", "en")],
    "additionalProperties": False,
}

PROMPT = """You write structured literature-review notes for a PhD student in industrial engineering. Their research is agent-based modelling and simulation of stochastic-flow network (SFN) reliability: rule-based routing agents, Q-learning path agents, and carriers with risk preferences, benchmarked against Lin (2009)'s static routing policy and quickest-path reliability.

Analyse the paper below in twelve sections. For each section give a Traditional Chinese version (繁體中文, *_zh) and an English version (*_en):
background (motivation and gap), research_question, theory (theoretical or methodological framework; for technical papers describe the method framework rather than forcing a social-science theory), constructs (main constructs or model components), construct_def (how they are defined or classified), variables (data sources and how variables are identified), model (research model, assumptions or problem setting), method (methodology), findings, contributions (academic and practical), limitations (and future research), extended_ideas (2-3 concrete follow-up research ideas, connected to the student's SFN/agent research where it fits naturally).

Also give title_zh (a Chinese translation of the title) and 3-6 English keywords.

Rules:
- Use only what the title and abstract support. Do not invent numbers, datasets, results or limitations.
- When the abstract does not say something, write exactly "摘要未提及此項，需查閱完整全文。" in the zh field and "Not stated in the abstract; full text required." in the en field. Inferences must be labelled as inferences (「推論：」/ "Inference:").
- If there is no abstract, most sections will be "not stated"; keep title-based statements cautious.
- extended_ideas may go beyond the paper, since they are suggestions.
- Keep each field to 1-4 sentences.

Paper:
"""

LOCK = threading.Lock()


def norm_title(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def page_arrays():
    """Curated scholar lists embedded in docs/index.html."""
    h = open(PAGE, encoding="utf-8").read()
    out = {}
    for key, name in (("lin", "SCHOLAR_LIN"), ("chang", "SCHOLAR_CHANG"), ("yeh", "SCHOLAR_YEH"), ("huang", "SCHOLAR_HUANG")):
        m = re.search(r"const " + name + r" = (\[.*?\n\]);", h, re.S)
        if m:
            out[key] = json.loads(re.sub(r",\s*\]$", "]", m.group(1)))
    curated_titles = set()
    m = re.search(r"const DEEP_ARTICLES = (\[.*?\n\]);", h, re.S)
    if m:
        try:
            curated_titles = {norm_title(a.get("title_en")) for a in json.loads(m.group(1))}
        except json.JSONDecodeError:
            pass
    return out, curated_titles


def candidates(radar, cfg):
    """Every tracked paper, in priority order, deduplicated by DOI/title."""
    journals = {j["id"]: j["name"] for j in cfg["journals"]}
    seen, out = set(), []

    def add(rec):
        k = rec["doi"] or norm_title(rec["title"])
        t = norm_title(rec["title"])
        if not t or k in seen or t in seen:
            return
        seen.add(k)
        seen.add(t)
        out.append(rec)

    for a in radar.get("feed", []):
        add({"doi": a.get("doi", ""), "title": a["title"], "authors": a.get("authors", ""),
             "journal": a.get("journal") or journals.get(a.get("j"), ""), "j": a.get("j", ""),
             "year": int((a.get("online") or a.get("added") or "0")[:4] or 0), "abstract": a.get("abstract", ""),
             "src": "feed"})
    for tid, tp in (radar.get("topics") or {}).items():
        for w in tp.get("items", []):
            add({"doi": w.get("doi", ""), "title": w["title"], "authors": w.get("authors", ""),
                 "journal": w.get("venue", ""), "j": "", "year": w.get("year") or 0, "abstract": "",
                 "src": "topic:" + tid})
    curated_keys = {"lin", "chang", "yeh", "huang"}  # same rule as the page: curated list + Crossref works from 2025
    for key, s in (radar.get("scholars") or {}).items():
        for w in s.get("works", []):
            if key in curated_keys and (w.get("year") or 0) < 2025:
                continue
            doi = (w.get("doi") or "").lower().replace("https://doi.org/", "")
            add({"doi": doi, "title": w["title"], "authors": w.get("authors", ""), "journal": w.get("venue", ""),
                 "j": "", "year": w.get("year") or 0, "abstract": "", "src": "scholar:" + key})
    arrays, curated = page_arrays()
    for key, rows in arrays.items():
        for r in rows:
            add({"doi": "", "title": r[1], "authors": r[2], "journal": r[3], "j": "", "year": r[0],
                 "abstract": "", "src": "scholar:" + key})
    return [c for c in out if norm_title(c["title"]) not in curated]


def find_doi(rec):
    q = urllib.parse.quote(rec["title"])
    data = ur.get_json(f"https://api.crossref.org/works?rows=3&query.bibliographic={q}&select=DOI,title")
    time.sleep(1.1)
    for it in (data or {}).get("message", {}).get("items", []):
        if norm_title(" ".join(it.get("title") or [])) == norm_title(rec["title"]):
            return it["DOI"].lower()
    return ""


def find_abstract(rec):
    doi = rec["doi"]
    if not doi:
        return ""
    data = ur.get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")
    time.sleep(1.1)
    ab = ur.clean_abstract(((data or {}).get("message") or {}).get("abstract"))
    if ab:
        return ab
    w = ur.get_json(ur.openalex_url(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}?select=abstract_inverted_index"))
    inv = (w or {}).get("abstract_inverted_index") or {}
    if inv:
        pos = sorted((i, word) for word, idx in inv.items() for i in idx)
        return ur.clean_abstract(" ".join(word for _, word in pos))
    s2 = ur.get_json(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{urllib.parse.quote(doi)}?fields=abstract", tries=2)
    time.sleep(1.1)
    return ur.clean_abstract((s2 or {}).get("abstract"))


def analyse(client, model, effort, rec):
    import anthropic
    body = (f"Title: {rec['title']}\nAuthors: {rec['authors']}\nVenue: {rec['journal']}\nYear: {rec['year']}\n"
            f"Abstract: {rec['abstract'] or '(no abstract available)'}")
    try:
        resp = client.beta.messages.create(
            model=model, max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": PROMPT + body}],
        )
    except anthropic.RateLimitError:
        time.sleep(30)
        return None
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        ur.log(f"  Claude error for '{rec['title'][:50]}': {type(e).__name__}")
        return None
    if resp.stop_reason == "refusal":
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def main():
    cfg = json.load(open(ur.CONFIG, encoding="utf-8"))
    dcfg = cfg.get("deep", {})
    if not dcfg.get("enabled", True):
        ur.log("deep analysis disabled in config")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        ur.log("ANTHROPIC_API_KEY not set: skipping deep analysis")
        return
    import anthropic

    radar = json.load(open(ur.OUT_JSON, encoding="utf-8"))
    done = json.load(open(OUT_JSON, encoding="utf-8")) if os.path.exists(OUT_JSON) else {"items": []}
    have = {norm_title(a["title_en"]) for a in done["items"]}
    todo = [c for c in candidates(radar, cfg) if norm_title(c["title"]) not in have]
    limit = int(os.environ.get("DEEP_MAX") or dcfg.get("max_per_run", 40))
    batch = todo[:limit]
    ur.log(f"deep analysis: {len(todo)} papers waiting, analysing {len(batch)} this run")
    if not batch:
        return

    for rec in batch:  # Crossref/OpenAlex/S2 lookups are rate-limited, so run them serially
        if not rec["doi"]:
            rec["doi"] = find_doi(rec)
        if not rec["abstract"]:
            rec["abstract"] = find_abstract(rec)

    client = anthropic.Anthropic()
    model = dcfg.get("model", "claude-opus-5-5")
    effort = dcfg.get("effort", "medium")
    today = dt.datetime.now(ur.TAIPEI).date().isoformat()
    added = 0

    def work(rec):
        return rec, analyse(client, model, effort, rec)

    with cf.ThreadPoolExecutor(max_workers=int(dcfg.get("concurrency", 4))) as pool:
        for rec, res in pool.map(work, batch):
            if not res:
                continue
            item = {"id": "auto-" + (rec["doi"] or norm_title(rec["title"])[:40]), "j": rec["j"] or "x",
                    "journal": rec["journal"] or "—", "year": rec["year"] or None,
                    "title_en": rec["title"], "doi": f"https://doi.org/{rec['doi']}" if rec["doi"] else None,
                    "full_text_accessed": False, "basis": "abstract" if rec["abstract"] else "title",
                    "src": rec["src"], "added": today, "auto": True}
            item.update(res)
            with LOCK:
                done["items"].append(item)
                added += 1

    done["updated"] = dt.datetime.now(ur.TAIPEI).isoformat(timespec="minutes")
    done["waiting"] = len(todo) - added
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(done, f, ensure_ascii=False, indent=1)
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("window.RADAR_DEEP = " + json.dumps(done, ensure_ascii=False) + ";\n")
    ur.log(f"deep analysis: added {added}, {done['waiting']} still waiting")


if __name__ == "__main__":
    main()
