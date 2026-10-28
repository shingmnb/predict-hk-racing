# 原始資料獲取教學

這份教學由零開始示範：**怎樣取得原件、證明它們在分析截止前已存在、建立賽前快照、加入帶 quote 的情報，再匯入及重現。**目標是先做好一場，不是一次過抓最多資料。

## 1. 開始前的界線

- 只使用合法、公開且允許保存的資料；不要繞過登入、付費牆、Captcha、速率限制或技術封鎖。
- 不要把本機檔案時間當成官方發布時間；內容上的「資料截至」也不等於首次可用時間。
- 不要把賽果頁最終賠率寫成 T-60 賠率，不要在已知賽果後補做「當日盲選」。
- 原始用戶資料放在工作區 `racing-data/`，不要寫入 Skill 安裝目錄或提交到 GitHub。
- 網址失效時，從 HKJC 主站導航到同名頁面，不要猜 URL；仍無法取得就記錄缺口。

消息分級見 [intel-sources.md](intel-sources.md)，完整 JSON 與命令見 [schemas-and-cli.md](schemas-and-cli.md)。

## 2. 準備一場的目錄與時間

先只選一場，記錄：

```text
race_id       = YYYY-MM-DD-ST-R1（或 HV）
venue         = ST / HV
off_time      = 官方開跑時間，ISO-8601、+08:00
data_cutoff   = 本次分析實際截止時間，不得遲於 off_time
collection_mode = live（真正賽前）或 historical（歷史重建）
```

建議目錄：

```text
racing-data/
├── sources/<race_id>/
│   ├── racecard/          # 原 PDF／HTML、派生文本、來源紀錄
│   ├── horses/            # 逐馬往績原件
│   ├── intelligence/      # 訪問、獸醫、研訊、試閘、操練
│   ├── market/            # T-60／T-10 原始頁面、截圖及轉錄
│   ├── results/           # 官方賽果與派彩
│   └── provenance/        # 可用性證明、manifest、哈希
├── cards/
├── claims/
├── results/
├── predictions/
└── historical.db          # 歷史重建與 live 庫分開
```

`<SKILL>` 代表 `predict-hk-racing` 目錄。先初始化獨立資料庫：

```bash
python "<SKILL>/scripts/racing_lab.py" init --db "racing-data/racing.db"
```

## 3. 第一步：取得排位與逐馬往績原件

### 3.1 保存排位原件

從 HKJC 排位頁下載實際場次的排位 PDF／HTML：<https://racing.hkjc.com/zh-hk/local/information/racecard>。

1. 核對日期、馬場、場次、開跑時間及路程。
2. 先保存原件，再提取文字；不要只保留整理後表格。
3. 記錄實際 `retrieved_at`、官方內容時點及首次觀察到檔案可用的時間。
4. 保存全場馬號、馬名、馬匹 ID、騎練、檔位、負磅、評分、配備、退出及後備馬。
5. 若頁面其後更正，新增版本；不要覆蓋舊原件。

### 3.2 保存逐馬原始往績

由官方馬匹資料／往績入口逐匹打開原始頁，不以整理後近績表代替。保存上一仗的：

- 馬匹 ID、賽事日期、場次及官方賽事連結；
- 騎師、練馬師、配備、班次、路程、場地、負磅及檔位；
- 名次、頭馬距離、沿途走位、完成／分段時間（如原文提供）；
- 競賽事件、研訊、獸醫或其他可解釋表現的原始紀錄。

每一項整理欄位都要能回到原頁行或逐字節錄。找不到上仗配備時填「未知」，不要由今仗配備反推。

### 3.3 計算原件 SHA-256

```bash
python -c "import hashlib,pathlib; p=pathlib.Path('racing-data/sources/<race_id>/racecard/original.pdf'); print(hashlib.sha256(p.read_bytes()).hexdigest())"
```

把哈希、相對路徑、URL、`retrieved_at` 及可用性證明寫入來源紀錄。哈希只證明內容一致，**不單獨證明檔案在賽前已存在**。

## 4. 第二步：先做基本面與情報，後看賠率

### 4.1 建立上仗／今仗對比

| 馬匹 | 欄位 | 上仗原文 | 今仗原文 | 可觀察變化 | 不可以直接推論 |
|---|---|---|---|---|---|
| 示例 | 配備 | 逐字保存 | 逐字保存 | 加／除／更換某配備 | 一定更集中、一定去馬 |
| 示例 | 騎師 | 逐字保存 | 逐字保存 | A 換 B | B 是主動選鞍、一定較重視 |

騎師 claim 至少要核查同倉其他出賽馬、過往組合、可選騎師、體重、檔期及直接訪問。只有 A 換 B 時，應寫「騎師變更；原因未知」。

### 4.2 建立一條可審計 claim

將讀到的完整段落放在 `source.excerpt`，再從中逐字複製 `quote`。以下只是結構示例，不是真實馬房消息：

```json
{
  "signal": "declared_gear_change",
  "direction": "neutral",
  "strength": 0.2,
  "confidence": 0.8,
  "source_id": "HKJC_RACECARD",
  "quote": "<由 source.excerpt 逐字複製的官方配備文字>",
  "note": "觀察：今仗配備與上仗不同。推論方向未確定；需核查相反解釋並以時間外消融驗證。"
}
```

- 寫不出逐字 quote，就標「證據不足」，不要轉述成原話。
- `direction`、`strength` 不是事實；未經驗證不得套用固定加權。
- 每項正面 claim 同時保存反證、未知及「出現甚麼證據就撤回」。
- 首輪至少抽 30 條（不足則全檢），由不知賽果且未見賠率的獨立標籤者盲審；報一致率及分歧。

### 4.3 封存基本面版本

在第一次打開賠率頁之前，保存短名單、支持／反證、替代劇本及：

```text
fundamental_created_at = 實際形成時間
market_blind           = true（只有確實未見任何賠率才可使用）
```

若早已看過賠率，就如實記 `market_blind: false`。封存後不要因熱門、冷門或落飛改寫原本理由。

## 5. 第三步：取得 T-60／T-10 全場賠率

### 5.1 原始頁面方式（優先）

1. 從 HKJC 官方賽事頁進入獨贏／位置賠率，不要混用其他場次或其他時點。
2. 核對日期、馬場、場次、頁面更新時間及實際抓取時間。
3. 同一輪保存全場所有 active 馬的 WIN／PLA；退出馬保留狀態，不當有價馬。
4. 在網站允許下保存原始 HTML／回應，另保存整頁截圖作視覺核對。
5. 為原始檔計 SHA-256，並記錄 URL、頁面時間、`retrieved_at`、`data_cutoff`。
6. T-10 另存一份新快照；不要覆蓋 T-60。

如果頁面只有部分馬、更新時間不明或抓取失敗，就標市場資料不完整；不要從其他時點補齊成「同一快照」。

### 5.2 截圖／人工轉錄方式（後備）

每張截圖應包含場次身份、頁面更新時間及完整馬匹範圍；一場多張圖要保存順序及重疊馬號供核對。

1. 保留未編輯原圖並計 SHA-256。
2. OCR 只可作初步轉錄；小數點、馬號及馬名要逐項對圖核對。
3. 最好由第二人獨立轉錄；沒有第二人時，隔開一次由原圖重新轉錄再比對，不要只重讀自己的表格。
4. 保存轉錄檔、核對時間、核對方式及差異修正紀錄。
5. 標明這是「頁面轉錄」，不是可重放的原始賠率 API 回應。

把數值寫入 race-card 對應 runner，例如：

```json
{
  "horse_no": 2,
  "horse_name": "<與排位原件一致>",
  "scratched": false,
  "win_odds": 5.9,
  "place_odds": 2.6
}
```

這只是 runner 片段，不是可單獨匯入的完整 race-card。

### 5.3 匯入與凍結情報分析

完成整份 race-card 後：

```bash
python "<SKILL>/scripts/racing_lab.py" import-card \
  --db "racing-data/racing.db" \
  --file "racing-data/cards/<race_id>_<snapshot>.json"

python "<SKILL>/scripts/racing_lab.py" analyze \
  --db "racing-data/racing.db" \
  --race-id "<race_id>" \
  --output "racing-data/analyses/<race_id>_<snapshot>.json"
```

輸出保存逐匹情報、反證、未知項及原始賠率快照，不附勝出率或公平倍數。T-10 必須新增 card snapshot 和 analysis run；開跑後不得補做 live 分析或修改舊輸出。只有另行訓練並通過回測的模型才使用 `predict`。

## 6. 第四步：賽後取得官方結果並結算

從官方賽果頁保存原始 HTML／PDF：<https://racing.hkjc.com/zh-hk/local/information/localresults>。

- 保存全部 active 馬的名次或合法非完賽狀態、退出馬、官方派彩及競賽事件。
- 平頭按原文保存；退出馬不得有名次。
- 最終獨贏賠率只可另存 `final_win_odds_reference`／收市參考，不回填賽前卡。
- 賽果更正要新增 result snapshot，不覆蓋舊版。

```bash
python "<SKILL>/scripts/racing_lab.py" import-result \
  --db "racing-data/racing.db" \
  --file "racing-data/results/<race_id>.json"

python "<SKILL>/scripts/racing_lab.py" evaluate \
  --db "racing-data/racing.db" \
  --output "racing-data/evaluations/latest.json"
```

## 7. 歷史資料：怎樣灌入才算有效

按官方賽期及賽果逐場建立清單，每場分別保存排位、逐馬往績、可用性證明、市場及結果。歷史卡使用：

```json
{
  "schema_version": "1.1",
  "collection_mode": "historical"
}
```

如果任何來源實際在 `data_cutoff` 後才補採，卡片還要明確加入 `retrieval_after_cutoff: true`；這只容許事後重建，**不是可用性證明**，也不能進入 forward KPI，live 卡不得使用。

本計劃的歷史驗收另要求每個來源保存實際 `retrieved_at`、原始檔 SHA-256、`available_at <= data_cutoff`、`available_at <= retrieved_at` 及 `availability_evidence`。內容上的 `source_timestamp` 不能代替可用性證明；用它作證據時，還要附可在 excerpt 找到的逐字 quote。

| 歷史場次資料狀態 | 可以做甚麼 | 不可以做甚麼 |
|---|---|---|
| 合格賽前卡＋所需特徵＋原始市場快照＋結果 | 按模型定義列入相應歷史樣本及可選值博回測 | 冒充當日 live 預測或 forward KPI |
| 有賽前卡及結果，但缺市場 | 情報分析及不依賴市場嘅模型訓練／評估；明確列缺口 | 計入市場消息對照或值博回測 |
| 只有賽果／最終賠率 | 工程測試、結果及收市參考 | 當成完整 T-60 樣本，或補寫當時基本面盲選 |

建立 coverage ledger，逐場記 `eligible_for`、`excluded_reason`、每項特徵覆蓋及證據缺口。**200+ 下載場數不等於 200+ 有效樣本；不得為湊數放寬時點規則。**樣本達預先登記要求後，才按 [modeling-and-backtesting.md](modeling-and-backtesting.md) 做 `fit`、walk-forward、消融及 `promote`。

## 8. 其他原始資料怎樣取得

| 資料 | 官方／一手入口 | 保存與核對重點 |
|---|---|---|
| 晨操 | <https://racing.hkjc.com/zh-hk/local/information/localtrackwork> | 同日同組比較；保存馬匹、段落、時間及頁面時點 |
| 試閘 | <https://racing.hkjc.com/racing/information/english/Horse/Btresult.aspx> | 保存完整組別、騎師、距離、時間及評語，不只摘名次 |
| 獸醫紀錄 | <https://racing.hkjc.com/en-us/local/information/ovedatabase> | 保存公告日期、馬匹 ID、完整原文及首次可用時間 |
| 賽事／獸醫新聞 | <https://racingnews.hkjc.com/chinese/> | 保存全文及相關段落，分清官方報告與記者評論 |
| 賽事重播／分段 | 由 HKJC 賽果或賽事頁進入官方影像／分段 | 保存來源、時間碼及標籤規則；不要只留剪輯後片段 |
| 騎練直接言論 | 完整官方訪問、馬房公布、原始錄音／影片 | 保存完整語境與日期；媒體轉述只作尋找原文的線索 |

下載間隔要合理，尊重網站條款及 robots／存取限制；遇到限制就保存失敗紀錄並改用官方 PDF、用戶原件或人工流程，不要嘗試繞過。

## 9. 常見失敗與正確處理

| 情況 | 正確處理 |
|---|---|
| Captcha、登入、白名單或存取被拒 | 停止自動請求，保存錯誤與時間；改用合法公開原件或人工下載 |
| PDF「資料截至」早於賽日，但不知何時發布 | 內容時點照記，另找首次可用證明；找不到就標時點缺口 |
| 只有部分馬賠率 | 標原始市場資料不完整，不以其他時點補齊；情報分析照樣可做 |
| OCR 與頁面數字不同 | 以原件為準，保留修正紀錄；核心欄位未核對前停止匯入 |
| 後備馬 S1／S2 | 放 `reserve_runners`，不要當普通數字馬號 |
| 開跑後才找到舊資料 | 只可作 historical；必須另證明它在 cutoff 前已可用 |
| 已有賽果後想重跑舊分析 | 不得改寫舊 live 分析；新增明確標記的歷史重建並分開評估 |

## 10. 一場完成後的驗收清單

```text
□ 原件、URL、retrieved_at、available_at／證據及 SHA-256 已保存
□ data_cutoff <= off_time，所有特徵只使用截止前版本
□ 全場馬號、馬名、騎練、退出、後備及核心欄位已核對
□ 基本面在看賠率前封存；market_blind 沒有倒填
□ T-60／T-10 各自是獨立、同場全馬、同一時點原始快照
□ 每條情報 quote 都能在來源 excerpt 逐字找到，並有反證／未知
□ 最終賠率、名次及賽後評語沒有進入賽前卡
□ import、analyze、import-result 的舊版本均沒有被覆蓋
□ 另一位操作者可由原件與 manifest 重現相同輸出
□ 報告同時列出缺口、排除樣本及陰性結果，沒有宣稱保證盈利
```

先讓一場通過以上檢查，再複製流程到下一場；不要先追求批量，再回頭補證據。
