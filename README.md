# 臺北市國小課後社團公告監測系統

每天掃描臺北市政府教育局與各國小的公告頁，找出與「課後照顧／社團報名」相關的公告
（含 PDF／DOCX／ODT 附件），以規則式啟發法抽取「申請開始日／申請結束日」，
最後產生一份可直接用瀏覽器打開的 `index.html` 儀表板。

> ⚠️ 日期是**規則式抽取**，不是人工確認的結果。畫面上每筆資料都標示信心值與「原文片段」，
> 並對低信心／抓不到日期的公告加上「需人工複核」標記，請務必點開核對。

---

## 1. 快速開始

```powershell
# 建議 Python 3.11+（本專案以 3.12 驗證）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 掃描全部來源 → 寫入 data/watch.db → 產生 index.html
python run_daily.py
```

常用參數：

```powershell
python run_daily.py --only meps,japs     # 只跑指定來源（除錯）
python run_daily.py --no-attachments     # 不下載附件（快速驗證設定用）
python run_daily.py --max-items 10       # 每個來源最多處理 10 筆
python run_daily.py --db data/other.db --out out.html
python run_daily.py --reextract-only     # 不重新爬取，只用資料庫既有內容重跑抽取
```

> 調整 `watcher/extractor.py` 的規則後，用 `--reextract-only` 即可立刻重新抽取
> 並重產 `index.html`，不必再花時間掃全站。

結束後打開 `index.html` 即可（純前端、無外部 CDN、不需要伺服器）。

---

## 2. 專案結構

```
run_daily.py            單一入口：爬取 → 抽取 → 存檔 → 產出 index.html
manage.py               本機學校清單管理介面（Flask，127.0.0.1:8765），只編輯 YAML
requirements.txt
sources/schools.yaml    學校清單與全域設定（唯一需要人工維護的檔案）
watcher/
  config.py             讀取 YAML
  http.py               requests session、逾時、重試、robots、每站 1–2 秒隨機間隔
  models.py             Notice / Attachment 資料結構
  adapters/nss.py       臺北市「校園網站系統 NSS」來源（多數國小採用）
  adapters/ccms.py      臺北市政府 CCMS 來源（教育局）
  attachments.py        附件下載 + PDF(pdfplumber)/DOCX(python-docx)/ODT 解析
  extractor.py          申請期間抽取（民國年／西元年／範圍／單年份）
  storage.py            SQLite：去重、首次發現、最後更新、每次執行紀錄
  renderer.py           產生自包含 index.html
data/
  watch.db              SQLite 資料庫
  meta.json             NSS 公告看板資訊快取（7 天）
index.html              交付物：儀表板
```

資料表：

| 表 | 用途 |
| --- | --- |
| `notices` | 公告本體，以公告 URL 去重；`first_seen` / `last_seen` / `content_hash` |
| `attachments` | 附件，以 `(公告 URL, 附件 URL)` 去重並保存 `sha256` 與解析文字 |
| `extractions` | 每筆公告的抽取結果：開始、結束、原文片段、信心、規則名稱 |
| `runs` / `source_runs` | 每次執行的總結果與各來源成功／失敗原因 |

---

## 3. 新增一所學校

編輯 `sources/schools.yaml`，在 `sources:` 底下加一筆：

```yaml
  - id: xyzps                    # 唯一識別碼，建議用校網代碼
    name: 臺北市○○區○○國民小學
    type: nss                    # 多數臺北市國小是 nss
    base_url: https://www.xyzps.tp.edu.tw
```

`type: nss` 只需網址，程式會自動：

1. 抓 `{base_url}/nss/p/index`；
2. 從頁面內嵌的 `window.siteserverData.deployMap` 找出公告看板的 `mid`／`sid`；
3. 呼叫 `POST {base_url}/nss/site/main/storage/{mid}/{sid}/find/`
   帶 `{"option":{"page":n,"number":20,"sort":{"stime":-1}}}` 取得公告 JSON。

若是臺北市政府 CCMS 平台（有 `News.aspx?n=...&sms=...` 列表頁）：

```yaml
  - id: doe_taipei
    name: 臺北市政府教育局
    type: ccms
    list_url: https://www.doe.gov.taipei/News.aspx?n=9AA0CC873BD001A2&sms=2E90303507CA4A6B
```

**驗證新來源**：先跑 `python run_daily.py --only xyzps --max-items 5`。
若來源掛掉或改版，把該筆加上 `disabled: true` 與 `note:` 說明原因——
單一來源失敗不會讓整批中斷，失敗原因會寫進 `source_runs` 並顯示在網頁頁尾。

若某校看板名稱特殊，可用 `page_keys` 覆寫要抓的看板關鍵字：

```yaml
    page_keys: [最新消息, 校園公告]
```

---

## 4. 調整關鍵字與抽取行為

`sources/schools.yaml` 的 `settings:` 區塊：

| 設定 | 說明 |
| --- | --- |
| `keywords` | 主題關鍵字（比對標題＋內文＋附件文字），預設課後照顧／課後／社團／才藝／育樂營… |
| `page_keys` | NSS 要抓的公告看板名稱關鍵字（預設最新消息／公告／消息／快訊／課後／社團／行政） |
| `max_pages` / `max_items` | 每個來源翻幾頁、最多處理幾筆 |
| `lookback_days` | 只處理近 N 天公告（預設 400，避免抓到封存看板的老資料） |
| `interval_min` / `interval_max` | 同站請求間隔（預設 1–2 秒） |
| `timeout` / `retries` | 逾時秒數與重試次數 |
| `respect_robots` | 是否遵守 robots.txt（預設 true） |
| `max_attachment_mb` / `max_attachments_per_notice` | 附件大小上限與每筆上限 |

單一來源也可覆寫 `keywords`（例如只關心社團不關心營隊）。

抽取規則在 [`watcher/extractor.py`](watcher/extractor.py)：

* 支援 `113年3月1日`、`113/03/01`、`2024-03-01`、`113.03.01`
* 支援區間：`113年3月1日至113年3月15日`、`113年3月1日至3月15日`（結束日省略年份）、
  `3月1日至3月15日`（兩端省略年份，用文中民國年或公告年份推斷）、`即日起至3月8日`
  （開始日以公告日推算）
* 跨年自動處理（`12月1日至1月15日` → 結束日 +1 年）
* **只在日期區間附近出現「報名／申請／受理／登記／招生／期限／截止」等字樣時才採用**，
  並排除前面緊接「活動日期／上課日期／適用時間」等標籤的區間，以降低把活動日期誤判為申請期間。
* 抽不到就標示「日期未識別」，不硬猜。

---

## 5. 每日排程

### 雲端自動執行（GitHub Actions，推薦）

推到 GitHub 後由 `.github/workflows/daily.yml` 每天 **07:30（台北時間）** 自動執行，
結果直接發布成網頁，**對方電腦不用開機、不用裝 Python**。

* 排程：`cron: "30 23 * * *"`（UTC）＝ 台北 07:30
* 手動觸發：repo → Actions →「每日掃描」→ Run workflow
* 歷史去重：`data/watch.db` 由 Actions 快取保存（不進 git，避免 repo 膨脹）
* 每次執行只提交 `index.html`，GitHub Pages 自動更新
* 網站網址：`https://<帳號>.github.io/<repo>/`

要改成其他時間，只改 cron 的兩個數字即可（UTC = 台北 − 8 小時）。

### Windows 工作排程器（本機執行）

```powershell
# 以管理員身分執行一次（建立每天 07:30 的排程）
schtasks /create /tn "TaipeiAfterSchoolWatch" /tr "C:\path\to\.venv\Scripts\python.exe C:\path\to\run_daily.py" /sc daily /st 07:30 /rl highest
```

圖形介面：工作排程器 → 建立基本工作 → 每日 → 啟動程式 → 
程式：`.venv\Scripts\python.exe`，引數：`run_daily.py`，起始位置：專案資料夾。

### Linux / macOS cron

```cron
30 7 * * * cd /path/to/project && ./.venv/bin/python run_daily.py >> logs/run.log 2>&1
```

建議把 `data/watch.db` 與 `index.html` 一起備份或放進排程輸出目錄。

---

## 6. 已知限制（請務必閱讀）

1. **覆蓋率約 22%**：目前設定 35 個來源（31 個啟用、4 個停用）：教育局 1 個＋30 所國小。
   臺北市國小約 140 所，其中約 100 所為 NSS 平台，理論上可用同一支 adapter 擴充；
   本版已逐校實測 30 所並確認每所至少抓到 1 筆公告，其餘學校尚未驗證。
   已納入來源分布在松山、信義、大安、中山、中正、大同、萬華、文山、內湖、北投等行政區，
   尚缺士林、南港、信義（部分）與多數北投／內湖學校。
2. **被阻擋／無法自動化的來源**：`schools.yaml` 中以 `disabled: true` 標記並註明原因：
   * 松山國小、三民國小：實測站台連線失敗（DNS 無法解析，待確認新網址）。
   * 中正國小、大佳國小：校網雖為 CCMS，但沒有自有公告頻道，僅轉載教育局公告。
3. **掃描階段確認連線失敗、故未納入的學校**（可作為後續補強清單）：
   西松（sups）、懷生（wses）、濱江（bjes）、大同（ttps）、雙園（syps）、老松（tlsps）
   DNS 解析失敗；博嘉實驗（bjps）、萬福（wfps）、南港（nkps）、麗山（lnps）連線逾時。
4. **抽取準確度**：規則是啟發法，會有兩類誤差：
   * 誤抓：把「活動／上課／繳費日期」當成申請期間（已用前後文標籤過濾，但仍可能漏網）。
   * 漏抓：日期只寫在附件表格內、使用中文數字（三月一日）、或只寫截止日而沒有起始日。
   * 表格排版（例如「報名開放日 10/23、活動日期 1/25~1/29」）最容易誤判，務必看原文片段。
5. **附件解析**：支援 PDF、DOCX、ODT/ODS；**舊版 `.doc`／`.xls` 與掃描式（圖片）PDF 不解析**，
   掃描檔會下載但抽不到文字（`attachments.ok = 0`）。
6. **NSS 平台內部 API 非官方公開介面**：站方改版可能導致 `deployMap` 或 `find` 端點失效，
   屆時該校會回報失敗，需要更新 `watcher/adapters/nss.py`。
7. **只掃描公開頁面**，遵守 robots.txt、每站 1–2 秒間隔；不處理需要登入的內容。

---

## 7. 儀表板功能

* 表格欄位：學校、公告標題、公告日期、申請開始、申請結束、狀態、附件連結＋原文片段
* 狀態自動依今日推算：進行中／即將開始／已截止／日期未識別
* 關鍵字搜尋（含原文片段）、狀態篩選、只看需人工複核、依申請結束日／開始日／公告日／學校排序
* 附件連結直接可點；頁尾列出各來源成功／失敗狀態

`index.html` 内的資料是產生當下的快照，重新執行 `run_daily.py` 才會更新。

---

## 8. 用管理頁維護學校清單（選用）

不想手改 YAML 時，可啟動本機管理頁：

```powershell
python manage.py                 # 自動開啟 http://127.0.0.1:8765/
python manage.py --no-browser    # 背景執行，不開瀏覽器
python manage.py --port 8777     # 換 port
```

介面功能：搜尋／排序來源、新增與編輯（含即時欄位驗證）、啟用／停用、刪除（二次確認）、
每列「測試」按鈕（只抓公告列表，不寫 DB、不下載附件），以及唯讀顯示 `data/watch.db` 的公告數與最後執行時間。

注意事項：

* 只綁定 `127.0.0.1`，不會對外開放；這是本機工具，不要曝露到區網或公網。
* **每次寫入前會備份**成 `sources/schools.yaml.bak.<YYYYmmdd-HHMMSS>`（同一秒多次寫入會加 `-2`、`-3` 後綴），
  寫入採「暫存檔 + 原子替換」，並在寫入後用正式 `Config` 載入器驗證，失敗自動回滾。
* 使用 ruamel.yaml round-trip：**檔案開頭說明、各段註解與空行都會保留**；
  既有項目的 `max_pages` 等進階欄位在編輯時不會被刪除。但**被刪除／被清空的欄位，其附註會一併消失**。
* 表單中的「CSS selector 覆寫」目前僅保存於設定檔，`nss`／`ccms` 轉接器尚未讀取，屬預留欄位。
* 刪除來源只刪設定，**不會刪除資料庫中該來源的歷史公告**（列表上會顯示筆數提醒）。
* 備份檔不會自動清理，需要時可自行刪除舊的 `.bak.*`。

**與每日排程的關係**：排程仍然直接呼叫 `run_daily.py`，管理頁只是編輯同一份 `sources/schools.yaml`。
兩者不衝突：管理頁不會觸發爬蟲，`run_daily.py` 下次執行時自然會讀到最新設定；
若在 `run_daily.py` 執行中存檔，該次執行可能仍使用舊設定，下次執行即生效。
