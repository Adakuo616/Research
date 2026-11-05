# 可靠度研究雷達（Research Radar）

追蹤產業工程、可靠度、專案管理、模擬與 Agent 相關期刊的每日新文章，加上四位學者的著作脈絡、深度文獻分析與 Agent 研究脈絡。

- 網站：<https://adakuo616.github.io/Research/>（開啟 GitHub Pages 後生效，見下方步驟）
- 每天台北時間 06:17 由 GitHub Actions 自動更新

## 每天自動做什麼

`scripts/update_radar.py` 會：

1. 依 `config/sources.json` 的期刊 ISSN，從 Crossref 抓最近 7 天新上線的文章。
2. 用關鍵字標籤（agent、reliability、risk、simulation、pm、sfn）比對標題與摘要，只保留至少符合一個標籤的文章。
3. Crossref 沒有摘要時（例如 Elsevier 期刊），改從 OpenAlex 補摘要。
4. 若設定了 `ANTHROPIC_API_KEY`，用 Claude 為每篇寫一到兩句繁體中文重點與一句英文摘要。
5. 依 ORCID 或英文姓名從 Crossref 抓每位追蹤學者的完整著作清單（以主題關鍵字過濾同名雜訊）；引用數、h-index、i10-index 優先取 OpenAlex，取不到時改用 Crossref 引用數計算（通常低於 Google Scholar，頁面上會標示來源）。
6. 結果寫入 `docs/data/radar.json` 與 `docs/data/radar-data.js`，由 workflow 自動 commit。

## 第一次設定（約 3 分鐘）

1. **開啟 GitHub Pages**：Settings → Pages → Build and deployment → Source 選 **Deploy from a branch**，Branch 選預設分支、資料夾選 **/docs**，按 Save。
2. **（選用）中文摘要**：Settings → Secrets and variables → Actions → New repository secret，名稱 `ANTHROPIC_API_KEY`。沒有設定時仍會每天更新，只是沒有中文摘要。
3. **（建議）OpenAlex 金鑰**：OpenAlex 的免費額度是依 IP 共用，GitHub Actions 很容易用完。到 openalex.org 申請免費 API key，存成 secret `OPENALEX_API_KEY`。沒有金鑰時學者指標會改用 Crossref 計算，文章摘要補抓也會略過。
4. **跑第一次**：Actions → Daily radar update → Run workflow。

## 加入期刊、關鍵字或學者

三種方式，擇一即可：

- **網站上**：「每日更新」分頁右側的「加入每日追蹤」，填好後按「送到 GitHub 加入」，會開啟預先填好的 Issue，按 Submit 即可。workflow 會自動寫入設定、回覆並關閉 Issue，然後重新抓取。只有儲存庫擁有者送出的 Issue 會被處理。
- **GitHub 上**：Issues → New issue → 選「新增追蹤期刊、關鍵字或學者」。學者需填顯示名稱與英文姓名，ORCID 選填（建議填，可避免同名誤判）。
- **直接改設定檔**：編輯 `config/sources.json` 後 commit，workflow 會自動重新抓取。

網站上的「我的關鍵字」只存在你的瀏覽器裡，用來立即標亮與篩選，不會改變每天抓取的範圍。

## 本機執行

```bash
pip install -r requirements.txt
python scripts/update_radar.py --no-llm
```

然後用瀏覽器開啟 `docs/index.html`。

## 檔案

| 路徑 | 用途 |
|---|---|
| `config/sources.json` | 追蹤的期刊、關鍵字標籤、學者 OpenAlex ID、摘要模型設定 |
| `scripts/update_radar.py` | 每日抓取與摘要 |
| `scripts/apply_issue.py` | 把 Issue 表單寫入設定檔 |
| `.github/workflows/daily-update.yml` | 每日排程、手動執行、設定變更時自動執行 |
| `.github/workflows/add-source.yml` | 處理「新增追蹤」Issue |
| `docs/index.html` | 儀表板（GitHub Pages） |
| `docs/data/` | 自動產生的資料 |
