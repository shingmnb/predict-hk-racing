# 消息來源與分析步驟

情報層要答嘅問題係：**邊啲消息真係會改變勝出概率，邊啲只係噪音？** 呢份文件列出來源、每類消息對應的 claim、賽日 SOP，以及怎樣證明消息有價值。

## 來源分級

### A 級：官方、可逐字引用

| 來源 | 內容 | 更新時點 | claim_type |
|---|---|---|---|
| 排位表 <https://racing.hkjc.com/zh-hk/local/information/racecard> | 全場、檔位、負磅、評分、配備、退出 | 排位日（T-1） | `gear_change`、`scratching` |
| 晨操資料 <https://racing.hkjc.com/zh-hk/local/information/localtrackwork> | 操練時間、段落、馬房 | 每日 | `trackwork` |
| 試閘結果 <https://racing.hkjc.com/racing/information/english/Horse/Btresult.aspx> | 名次、距離、騎師、評語 | 每次試閘後 | `trial_performance` |
| 傷患／健康紀錄 <https://racing.hkjc.com/en-us/local/information/ovedatabase> | 獸醫紀錄、傷患、健康狀況 | 持續 | `vet` |
| 賽馬新聞 <https://racingnews.hkjc.com/chinese/> | 獸醫報告增補、賽事報告、馬房消息 | 每日 | `vet`、`steward_report` |
| 貼士指數 <https://racing.hkjc.com/racing/chinese/tipsindex/tips_index.asp> | 傳媒貼士彙總 | 賽日 | `consensus`（市場情緒，非意圖） |
| 賽果與分段 <https://racing.hkjc.com/zh-hk/local/information/localresults> | 名次、走位、段速、派彩 | 賽後 | `trip`、`result` |

### B 級：專業媒體（要兩個獨立來源互證）

- SCMP Racing <https://www.scmp.com/sport/racing>
- At The Races（香港）<https://www.attheraces.com/tips/atr-tipsters/tim-carroll>
- 其他有署名、有日期、可追溯的賽馬媒體

### C 級：社群與傳聞

Telegram／X 貼士頻道、YouTube 馬評、討論區。**永遠唔可以直接入模型**，只可以寫入報告的「傳聞」欄，等有 A/B 級佐證才升級。

## 來源 → claim 對應

| 觀察到什麼 | claim_type | 最小證據 |
|---|---|---|
| 配備首次加／除（眼罩、鼻箍、綁舌帶） | `gear_change` | 排位表原文 |
| 騎師由 A 換 B，或同倉選鞍 | `jockey_choice` | 排位表 + 訪問（如有） |
| 試閘贏／輸幾個馬位、時間 | `trial_performance` | 試閘結果頁 |
| 晨操快跳時間、與同組比較 | `trackwork` | 晨操頁 + 同日同組 |
| 獸醫紀錄、賽後檢查 | `vet` | 官方紀錄原文 |
| 馬房訪問原句 | `stable_comment` | 逐字 quote + 媒體名 + 日期 |
| 賽事報告的解釋（例如「全程外疊」） | `steward_report` | 官方賽事報告 |
| 賠率大幅變動（早飛／落飛） | `market_move` | 同一時點賠率快照 + 時間 |
| 傳媒一致看好 | `consensus` | 貼士指數／至少三家 |

## 賽日 SOP

### T-3：場次與結構
- 賽期、場地、場次、班次、路程、獎金
- 同程／同班往績基準（唔好靠記憶）
- 跑道選用、預期天氣

### T-1（排位後，最重要）
- 全場：檔位、負磅、評分、配備
- **配備改動 vs 上一仗**（逐匹對比）
- **騎師選鞍**：同倉其他馬由邊個騎
- 退出／後備補上
- 每匹 90 日內傷患紀錄
- 上次賽後報告有冇解釋（外疊、受困、流鼻血）
- 試閘／晨操最新動態

### 賽日日間
- 場地狀況、度地儀、雨量
- 晨操最終報告
- 貼士指數（只作市場情緒）
- 臨場退出、臨時配備改動

### T-60 至 T-10
- 全場同時間點賠率快照
- 賠率跌超過 25% 的馬 → `market_move` claim
- 任何新消息（訪問、社交）

### 賽後
- 賽事報告：逐匹有冇解釋
- 段速：頭段／中段／末段對比
- 獸醫報告增補
- **把賽前 claim 逐條標記：證實 / 推翻 / 無法判斷**（唯一可建立意圖 ground truth 的方法）

## 由消息到概率的三條紀律

1. **去重**：同一新聞被十個媒體轉載 = 一個證據。
2. **時效**：`observed_at` 超過 14 日權重減半；超過 30 日只作背景。
3. **可證偽**：每條 claim 要寫「什麼情況下作廢」。

## 哪些消息真的有用

**高價值**：騎師選鞍、配備首次改動、試閘表現（有冇被催／末段有冇加速）、獸醫與傷患、賽後報告的解釋。

**中價值（要標準化）**：晨操時間（必須同同日同組比較）、貼士指數（與賠率高度共線，要 ablation）。

**陷阱**：「馬房有消息」但無出處、把轉載當多來源、用賽果反推意圖、賽日即場故事當信號。

## 怎樣證明消息有價值

```bash
fit ... --model-version market-only --features market_logp
fit ... --model-version intel-only  --features intent_centered,rating
fit ... --model-version combined \
  --features market_logp,intent_centered,rating --block intel=intent_centered
backtest --model-version combined --ablate intel
```

| 結果 | 結論 |
|---|---|
| intel-only 差過均勻模型 | 情報暫時無預測價值，只可做敍事 |
| intel-only 接近市場 | 情報有料，值得加大投入 |
| combined 贏 market 且 `--ablate intel` 顯著 | 情報有獨立價值，可以 promote |
| 只有 combined 贏、ablate 不顯著 | 贏的是市場特徵，情報只是裝飾 |

## 賽日一頁 checklist

```
□ 場地／跑道／天氣
□ 全場宣佈 + 退出
□ 配備改動（逐匹 vs 上仗）
□ 騎師選鞍（同倉對比）
□ 傷患紀錄（90 日）
□ 試閘／晨操（同組比較）
□ 馬房訪問原句（如有）
□ 賠率快照（T-60 / T-10）
□ 三條最強情報 + 一條反證
□ 偏離市場 >15pp 的理由（如適用）
```
