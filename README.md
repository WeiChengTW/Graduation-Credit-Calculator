# 長庚大學畢業學分檢查工具

這個工具會自動登入長庚 MOOCS 取得修課清單，透過長庚課程查詢 API 產生 `courses_detail.csv`，再把畢業規則 PDF 轉成 Markdown，交給 LLM 依規則分析目前已完成與尚缺的畢業條件。

流程：

1. 使用 MOOCS 帳密登入課程列表
2. 爬取每門課的 `學年-學期-課程名稱-開課序號`
3. 產生 `generated/moocs_courses.txt`
4. 呼叫長庚 catalog API 產生 `generated/courses_detail.csv`
5. 將畢業規則 PDF 轉成 `generated/rules_md/*.md`
6. 使用 MiniMax-M2.7-highspeed 分析畢業條件
7. 輸出 `generated/graduation_report.json` 與 `generated/graduation_report.md`

---

## 檔案說明

| 檔案 / 資料夾 | 說明 |
|---|---|
| [generate_courses_detail.py](generate_courses_detail.py) | 主程式 |
| [requirements.txt](requirements.txt) | Python 依賴套件 |
| [data/rules/](data/rules/) | 使用者提供的畢業、通識、榮譽學程等規則 PDF |
| [generated/](generated/) | 最新產生物，包含課程明細、轉換後規則、報告 |
| [archive_v1/](archive_v1/) | 舊資料與歷史輸出備份 |
| [scratch/](scratch/) | 臨時除錯檔，不屬於正式流程 |
| [logs/](logs/) | 執行或除錯 log |
| `generated/moocs_courses.txt` | 從 MOOCS 爬到的課程清單 |
| `generated/courses_detail.csv` | catalog API 查出的課程明細 |
| `generated/rules_md/` | 畢業規則 PDF 轉出的 Markdown |
| `generated/graduation_report.json` | LLM 回傳的結構化畢業檢查結果 |
| `generated/graduation_report.md` | 給人看的 Markdown 報告 |
| `generated/graduation_report.raw.txt` | LLM 回覆無法解析成 JSON 時的除錯檔 |

---

## 安裝

建議使用虛擬環境：

```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

需要的套件：

- `playwright`：登入 MOOCS、模擬瀏覽器操作
- `pymupdf` / `pymupdf4llm`：將 PDF 規則轉 Markdown
- `python-dotenv`：讀取 `.env`

---

## 環境變數

可以建立 `.env`，避免每次輸入 API key：

```bash
CGU_MOOCS_USER=bxxxxxxx
CGU_MOOCS_PASSWORD=你的MOOCS密碼
MINNIMAX_API_KEY=你的MiniMax API Key
```

也可以不放密碼在 `.env`，執行時由程式互動輸入。

注意：不要把 `.env` 上傳到公開 repo。

---

## 最完整用法

```bash
python3 generate_courses_detail.py \
  --output-dir runs/william-112-cs \
  --scrape-moocs \
  --moocs-user bxxxxxxx \
  --rules data/rules/畢業學分.pdf data/rules/榮譽學程學生手冊_112入學適用.pdf 'data/rules/112學年度下學期通識課程表--適用112學年度(含)後入學學生.pdf' \
  --analyze
```

如果沒有把 `MINNIMAX_API_KEY` 放進 `.env`：

```bash
python3 generate_courses_detail.py \
  --output-dir runs/william-112-cs \
  --scrape-moocs \
  --moocs-user bxxxxxxx \
  --rules data/rules/畢業學分.pdf data/rules/榮譽學程學生手冊_112入學適用.pdf 'data/rules/112學年度下學期通識課程表--適用112學年度(含)後入學學生.pdf' \
  --analyze \
  --llm-api-key '你的MiniMax API Key'
```

預設 LLM：

```text
MiniMax-M2.7-highspeed
```

預設 API base URL：

```text
https://minnimax.chat/v1
```

---

## 常用模式

### 1. 只登入 MOOCS 並產生課程明細

```bash
python3 generate_courses_detail.py \
  --scrape-moocs \
  --moocs-user bxxxxxxx
```

輸出：

- `generated/moocs_courses.txt`
- `generated/courses_detail.csv`

### 2. 有畫面模式登入 MOOCS

如果登入頁有驗證碼、跳轉異常，或想看瀏覽器操作：

```bash
python3 generate_courses_detail.py \
  --scrape-moocs \
  --moocs-user bxxxxxxx \
  --headful
```

### 3. 只把 PDF 規則轉 Markdown

```bash
python3 generate_courses_detail.py \
  --rules data/rules/畢業學分.pdf data/rules/榮譽學程學生手冊_112入學適用.pdf 'data/rules/112學年度下學期通識課程表--適用112學年度(含)後入學學生.pdf' \
  --convert-rules-only
```

輸出在：

```text
generated/rules_md/
```

### 4. 使用既有 `moocs_courses.txt` 產生 `courses_detail.csv`

這是 debug / 備援模式，不需要重新登入 MOOCS：

```bash
python3 generate_courses_detail.py \
  --input moocs_courses.txt \
  --output courses_detail.csv
```

### 5. 只用既有 `courses_detail.csv` 和規則檔做 LLM 分析

如果 `courses_detail.csv` 已存在，可以明確指定要重用它：

```bash
python3 generate_courses_detail.py \
  --output generated/courses_detail.csv \
  --reuse-existing-csv \
  --rules data/rules/畢業學分.pdf data/rules/榮譽學程學生手冊_112入學適用.pdf 'data/rules/112學年度下學期通識課程表--適用112學年度(含)後入學學生.pdf' \
  --analyze
```

如果沒有加 `--reuse-existing-csv`，程式會避免默默分析舊 CSV。

---

## 輸出報告

### `graduation_report.json`

給程式或前端使用的結構化結果，包含：

- 畢業狀態
- 已認列學分
- 尚缺學分
- 各類別完成狀況
- 尚缺項目
- 不採認或只能部分採認的課程
- 需要人工確認的項目
- 建議下學期修課

### `graduation_report.md`

給人看的報告，建議格式包含：

```markdown
# 畢業學分檢查報告

## 1. 一句話結論
## 2. 缺口總表
## 3. 完成狀況總表
## 4. 詳細檢查
## 5. 不採認或只能部分採認的課程
## 6. 需要人工確認
## 7. 建議下學期選課清單
## 8. 判斷依據
```

目前程式會要求 LLM 依照上傳的 PDF 規則動態判斷，不把資工系規則寫死在 Python 裡。LLM 回傳必須通過 JSON schema 驗證，否則不會產生正式報告，只會保存 `graduation_report.raw.txt` 供除錯。

---

## CLI 參數

### MOOCS / 課程資料

| 參數 | 說明 |
|---|---|
| `--scrape-moocs` | 登入 MOOCS 並爬課程清單 |
| `--moocs-user` | MOOCS 帳號，也可用 `CGU_MOOCS_USER` |
| `--moocs-password` | MOOCS 密碼，也可用 `CGU_MOOCS_PASSWORD` |
| `--headful` | 顯示瀏覽器畫面 |
| `--max-pages` | 最多爬幾頁，預設 `30` |
| `--output-dir` | 產生檔案集中輸出的資料夾，預設 `generated` |
| `--save-moocs` | MOOCS 清單輸出路徑，預設 `generated/moocs_courses.txt` |
| `--input`, `-i` | 離線模式輸入檔，預設 `moocs_courses.txt` |
| `--output`, `-o` | 課程明細 CSV，預設 `generated/courses_detail.csv` |
| `--delay` | catalog API 查詢間隔秒數，預設 `0.1` |
| `--term-map` | 補充學期代碼對照，可用 JSON 字串或 JSON 檔 |
| `--debug` | 顯示額外除錯資訊；不會印出密碼或 API key |
| `--reuse-existing-csv` | 允許分析時重用既有 `courses_detail.csv`，避免誤用舊資料 |

### 規則 PDF

| 參數 | 說明 |
|---|---|
| `--rules` | 畢業規則 PDF 路徑，可一次給多個 |
| `--convert-rules-only` | 只轉 PDF 成 Markdown，不做課程查詢與分析 |

### LLM 分析

| 參數 | 說明 |
|---|---|
| `--analyze` | 啟用 LLM 畢業學分分析 |
| `--llm-base-url` | LLM API base URL，預設 `https://minnimax.chat/v1` |
| `--llm-api-key` | LLM API key，也可用 `MINNIMAX_API_KEY` |
| `--llm-model` | LLM 模型，預設 `MiniMax-M2.7-highspeed` |
| `--report-output` | Markdown 報告輸出，預設 `generated/graduation_report.md` |
| `--report-json-output` | JSON 報告輸出，預設 `generated/graduation_report.json` |
| `--raw-report-output` | JSON 解析失敗時保存原始 LLM 回覆，預設 `generated/graduation_report.raw.txt` |

---

## 課程過濾規則

MOOCS 裡可能有不是正式課程的項目，例如：

```text
E-Learning 操作說明
```

程式會忽略這類項目，只保留符合下面格式的課程：

```text
學年-學期-課程名稱-開課序號
```

例如：

```text
114-2-人工智慧-A2342
113-2-資料庫系統設計-65436-合開課程
```

---

## 不同科系怎麼用

這個工具不把畢業規則寫死在程式裡。

如果朋友是不同科系或不同入學年度，只要替換 `--rules` 後面的 PDF：

```bash
python3 generate_courses_detail.py \
  --scrape-moocs \
  --moocs-user bxxxxxxx \
  --rules 朋友的畢業學分.pdf 朋友的通識課程表.pdf 朋友的其他抵免規則.pdf \
  --analyze
```

LLM 會根據這些 PDF 轉出的 Markdown 動態判讀規則。

---

## 學期代碼

程式內建目前常用 term id：

| 學年學期 | termid |
|---|---:|
| 112-1 | 63 |
| 112-2 | 64 |
| 112-3 | 65 |
| 113-1 | 66 |
| 113-2 | 67 |
| 113-3 | 68 |
| 114-1 | 69 |
| 114-2 | 70 |
| 114-3 | 71 |

如果未來學期不在表內，可用 `--term-map` 補充，例如：

```bash
python3 generate_courses_detail.py \
  --input moocs_courses.txt \
  --term-map '{"115-1": 72, "115-2": 73}'
```

---

## 常見問題

### 1. MOOCS 登入失敗

先用有畫面模式：

```bash
python3 generate_courses_detail.py --scrape-moocs --moocs-user bxxxxxxx --headful
```

可能原因：

- 帳密錯誤
- MOOCS 頁面改版
- 需要人工驗證
- 學校網站暫時無法連線

### 2. Playwright 找不到 Chromium

執行：

```bash
python3 -m playwright install chromium
```

### 3. PDF 轉 Markdown 失敗

確認已安裝：

```bash
python3 -m pip install pymupdf pymupdf4llm
```

如果 PDF 是掃描圖片，純文字轉換可能抓不到完整內容，需要先 OCR。

### 4. LLM 分析失敗

確認：

- `MINNIMAX_API_KEY` 是否正確
- `--llm-base-url` 是否為 `https://minnimax.chat/v1`
- 模型名稱是否為 `MiniMax-M2.7-highspeed`
- 規則 Markdown 是否成功產生

### 5. 報告說「無法判定」

這通常代表 PDF 規則不完整、課程領域對照不清楚，或某些抵免規則需要人工確認。請補充更多規則 PDF，或向系辦/通識中心確認。

---

## 安全注意

- 不要把 `.env`、API key、MOOCS 密碼上傳到 GitHub。
- `generated/`、`courses_detail.csv`、`moocs_courses.txt`、`graduation_report.*` 都可能含有修課紀錄或個資，預設已建議加入 `.gitignore`。
- 建議不要把 MOOCS 密碼放進 `.env`，可在執行時互動輸入。
- 程式只會把課程 CSV 與規則 Markdown 傳給 LLM 分析，不會把 MOOCS 密碼傳給 LLM。
- 本工具產出的報告不是官方畢業審查，正式資格仍以系辦或教務處認定為準。
