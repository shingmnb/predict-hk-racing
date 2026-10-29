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
- 賠率只記錄變動幅度與時間窗，另列 `market_move` 市場觀察；25% 之類門檻只可作預先登記的候選假設，未經時間外驗證不得當固定規則或意圖證據
- 任何新消息（訪問、社交）

### 賽後
- 賽事報告：逐匹有冇解釋
- 段速：頭段／中段／末段對比
- 獸醫報告增補
- **只核對 claim 的可觀察部分：事實是否準確、替代解釋是否出現、預測是否按原規則結算。**唔好用賽果把意圖標成「證實／推翻」；真正意圖 ground truth 通常不可取得

## 由消息到判斷的三條紀律

1. **去重**：同一新聞被十個媒體轉載 = 一個證據。
2. **時效**：記錄 `observed_at` 與每項資料的適用窗口；「14 日減半／30 日只作背景」只能列候選假設，不得未經時間外驗證自動套用固定權重。
3. **可證偽**：每條 claim 要寫「什麼情況下作廢」。

## 哪些消息真的有用

**優先核查（唔等於已證明有預測價值）**：騎師選鞍、配備首次改動、試閘表現（有冇被催／末段有冇加速）、獸醫與傷患、賽後報告的解釋。

**需要標準化**：晨操時間（必須同同日同組比較）、貼士指數（與賠率可能共線，要 ablation）。

**陷阱**：「馬房有消息」但無出處、把轉載當多來源、用賽果反推意圖、賽日即場故事當信號。

## 怎樣證明消息有價值

首先用 `analyze` 保存當時情報，唔需要為每場硬出概率。要驗證情報對勝率有冇增量，先用同一批合格歷史快照訓練兩個可選模型：

```bash
fit ... --model-version ability-only --features rating,draw
fit ... --model-version with-intel \
  --features intent_centered,rating,draw --block intel=intent_centered
backtest --model-version with-intel --ablate intel
```

| 結果 | 可以得出的結論 |
|---|---|
| with-intel 差過均勻無資訊基準 | 暫無證據支持情報有預測價值；保留陰性結果 |
| with-intel 接近 ability-only | 只值得繼續驗證，不等於情報有獨立價值 |
| with-intel 改善且 `--ablate intel` 顯著 | 只係情報增量嘅其中一項證據；仍須通過校準、Brier、多重比較、champion 及全部 promote 門檻 |
| 只有整體改善、ablate 不顯著 | 不可把改善歸因情報；不得宣稱情報有效 |

無論模型結果如何，都要保留事實核查、反證同未知；一場輸贏唔可以證明或推翻馬房真正意圖。

## 賽日一頁 checklist

```
□ 場地／跑道／天氣
□ 全場宣佈 + 退出
□ 配備改動（逐匹 vs 上仗）
□ 騎師選鞍（同倉對比）
□ 傷患紀錄（90 日）
□ 試閘／晨操（同組比較）
□ 馬房訪問原句（如有）
□ 三條最強情報 + 每條反證／未知／撤回條件
□ 可選原始賠率快照（T-60 / T-10），只記市場變化
□ analyze 已凍結；冇模型時沒有硬出勝率
```
