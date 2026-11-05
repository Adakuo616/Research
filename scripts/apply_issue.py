#!/usr/bin/env python3
"""Apply a "新增追蹤期刊或關鍵字" issue form to config/sources.json.

Reads the issue body from $ISSUE_BODY and prints GitHub Actions outputs
(changed, summary, message) on stdout.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "sources.json")
ISSN_RE = re.compile(r"^\d{4}-\d{3}[\dXx]$")
ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dXx]$")


def parse_form(body):
    """Issue forms render as '### Label\\n\\nvalue' blocks."""
    fields = {}
    for block in re.split(r"^###\s+", body or "", flags=re.M)[1:]:
        label, _, value = block.partition("\n")
        value = value.strip()
        fields[label.strip()] = "" if value == "_No response_" else value
    def find(prefix):
        return next((v for k, v in fields.items() if k.startswith(prefix)), "")
    return {"kind": find("類型"), "keyword": find("關鍵字"), "tag": find("歸入"),
            "journal": find("期刊名稱"), "issn": find("ISSN"),
            "scholar": find("學者顯示名稱"), "author": find("學者英文姓名"), "orcid": find("ORCID")}


def slug(text, taken):
    base = re.sub(r"[^a-z0-9]+", "", text.lower())[:8] or "src"
    out, i = base, 2
    while out in taken:
        out, i = f"{base}{i}", i + 1
    return out


def emit(changed, summary, message):
    print(f"changed={'true' if changed else 'false'}")
    print(f"summary={summary}")
    print("message<<EOF_MSG")
    print(message)
    print("EOF_MSG")


def main():
    form = parse_form(os.environ.get("ISSUE_BODY", ""))
    cfg = json.load(open(CONFIG, encoding="utf-8"))

    if form["kind"] == "學者":
        name, author = form["scholar"].strip(), form["author"].strip()
        orcid = re.sub(r"^https?://orcid\.org/", "", form["orcid"].strip())
        if not name or not author:
            emit(False, "", "沒有加入：請填學者顯示名稱與英文姓名。")
            return
        if orcid and not ORCID_RE.match(orcid):
            emit(False, "", f"沒有加入：ORCID 格式應為 0000-0000-0000-000X，收到「{orcid}」。")
            return
        for sc in cfg["scholars"]:
            if (orcid and sc.get("orcid") == orcid) or sc.get("author", "").lower() == author.lower():
                emit(False, "", f"「{sc['name']}」已在追蹤清單中，沒有重複加入。")
                return
        key = slug(author, {s["key"] for s in cfg["scholars"]})
        entry = {"key": key, "name": name, "author": author}
        if orcid:
            entry["orcid"] = orcid
        cfg["scholars"].append(entry)
        summary = f"scholar {author}"
        message = (f"已加入學者「{name}」（{author}{'，ORCID ' + orcid if orcid else ''}），正在重新抓取，"
                   "幾分鐘後「學者研究脈絡」會出現這位學者的著作與主題分類。"
                   + ("" if orcid else "沒有填 ORCID，會依英文姓名比對，可能混入同名作者的論文。"))
    elif form["kind"] == "期刊":
        name = form["journal"].strip()
        issns = [x.strip().upper() for x in re.split(r"[,，\s]+", form["issn"]) if x.strip()]
        bad = [x for x in issns if not ISSN_RE.match(x)]
        if not name or not issns or bad:
            emit(False, "", f"沒有加入：請填期刊名稱與正確格式的 ISSN（例如 1366-5545）。無法辨識：{', '.join(bad) or '未填'}")
            return
        for j in cfg["journals"]:
            if set(issns) & set(j.get("issn", [])):
                emit(False, "", f"「{j['name']}」已在追蹤清單中，沒有重複加入。")
                return
        jid = slug(name, {j["id"] for j in cfg["journals"]})
        cfg["journals"].append({"id": jid, "name": name, "issn": issns})
        summary = f"journal {name}"
        message = f"已加入期刊「{name}」（ISSN {', '.join(issns)}，代碼 {jid}），正在重新抓取，幾分鐘後儀表板會出現它的新文章。"
    else:
        terms = [t.strip().lower() for t in re.split(r"[,，]+", form["keyword"]) if t.strip()]
        if not terms:
            emit(False, "", "沒有加入：請在「關鍵字」欄位填入至少一個英文關鍵字。")
            return
        tag_in = form["tag"].strip() or "agent"
        tag = next((t for t in cfg["tags"] if t["id"] == tag_in.lower() or t["label"] == tag_in), None)
        if tag is None:
            tag = {"id": slug(tag_in, {t["id"] for t in cfg["tags"]}), "label": tag_in, "terms": []}
            cfg["tags"].append(tag)
        added = [t for t in terms if t not in tag["terms"]]
        if not added:
            emit(False, "", f"這些關鍵字已在「{tag['label']}」標籤中，沒有變更。")
            return
        tag["terms"].extend(added)
        summary = f"keywords {', '.join(added)} -> {tag['id']}"
        message = f"已把關鍵字 {', '.join(added)} 加入「{tag['label']}」標籤，正在重新抓取並重新標記文章。"

    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    emit(True, summary, message)


if __name__ == "__main__":
    sys.exit(main())
