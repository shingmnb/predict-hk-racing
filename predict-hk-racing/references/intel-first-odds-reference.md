# Intel-First, Odds-as-Reference（消息主導、賠率只作參考）

> 狀態：**RFC — 待實作（KIMI）**
> 日期：2026-10-26
> 關聯：`references/intent-model.md`、`references/modeling-and-backtesting.md`

## 1. 為何要改

現行 `auto` 模型在沒有 promoted model 時用 `market-no-vig-v1`，即**純去水市場**；即使 fit 出模型，標準化後 `market_logp` 佔咗 **65.3%** 絕對權重（實測 2026-10-25，18 個特徵）。

本機回測（687 場、69 個賽日、實際派彩）結果：

| 設定 | logloss | Top-1 | Top-3 |
|---|---:|---:|---:|
| 市場基準 | 2.0612 | 27.2% | 59.9% |
| 市場 + 基本面 + KG 特徵 | 2.0766 | 29.3% | 60.7% |
| **完全去掉賠率**（17 個非賠率特徵） | **2.3003** | **21.6%** | **48.3%** |

結論：
1. 賠率是目前最強單一信號，但**加任何現有特徵都贏唔到市場**（Δ logloss 的 95% CI 跨 0）。
2. 非賠率特徵有信息（Top-1 21.6% 高於隨機），但**信息量遠低於市場**。
3. 要令「消息主導」成立，消息必須係**獨家／及時**（試閘、配備、馬房動向、騎師選擇、獸醫），而唔係 PDF 上人人都有嘅往績。

## 2. 三層權重（初版，可 env 覆寫）

| 層 | 內容 | 權重 |
|---|---|---:|
| 消息／意圖 | 試閘、配備變動、馬房／騎師意圖、體重／健康、班次／路程轉變 | **50%** |
| 基本面 | 往績、班次、路程適性、騎練、檔位 | **30%** |
| 賠率 | 去水市場概率、落飛／回飛 | **20%** |

- 權重必須集中定義（建議 `INTEL_WEIGHT` / `FUNDAMENTALS_WEIGHT` / `ODDS_WEIGHT`），總和校驗 = 1。
- 呢組數字係**起始假設**，未經回測驗證；任何調整都必須走 §7 的回測協議。

## 3. 情報 schema（每匹馬）

新增 `intel_evidence[]`，每項必填：

```json
{
  "category": "trial | gear | stable_intent | jockey_intent | health_weight | class_distance",
  "direction": "positive | negative | neutral",
  "strength": 0.0,
  "confidence": 0.0,
  "source_id": "S1",
  "quote": "逐字原文，正規化後 >= 8 字",
  "observed_at": "2026-10-24T18:00:00+08:00"
}
```

規則：
1. `quote` 必須逐字出現於 `sources[].excerpt`，否則 `import-card` 拒絕（沿用 schema 1.1 防幻覺機制）。
2. 冇 `quote` 或 `source_id` 無效 → 該項**不計分**，並在輸出標「證據不足」。
3. `observed_at` 必須早於 `off_time`；賽後才出現的資料不得回填。

## 4. 賠率的三個用途

1. **驗證背馳**：消息強 + 賠率冷 → 機會；消息弱 + 賠率熱 → 陷阱。
2. **定價**：計算 EV、Kelly、彩池上限。
3. **時序確認**：落飛／回飛只作確認，不作唯一觸發。

## 5. 賠率否決權（收窄）

賠率只可以在以下兩種情況**降級**（不可直接刪除消息 pick）：

- 位置盤回軟 **≥12%**（同一 snapshot 對比，且已過 T-65 成熟池）
- SP 崩塌 **≥50%** 且最終 SP ≤5（臨場暴跌）

歷史反例：2026-10-25 R10，舊規則用「同程 <3 戰」否決了基本面第一的 #4 正本巨星，結果該駒以 1.6 倍贏馬。**賠率／樣本規則不得否決消息主導的選擇。**

## 6. 輸出格式（強制）

每個 pick 必須有兩欄：

| 欄位 | 要求 |
|---|---|
| `message_reason` | 18–50 字繁中，必須引用至少一項 `intel_evidence` 的 category 與事實 |
| `odds_reference` | 當時賠率、去水概率、EV；明確標示「參考」 |

冇 `message_reason` → 唔准出街（validator 拒絕）。

## 7. 回測協議

1. **A/B**：同一批賽事、同一 snapshot，比較「現行 65/35 賠率主導」vs「50/30/20 消息主導」。
2. 指標：logloss、Brier、Top-1、Top-3、位置命中率、Q/PQ ROI。
3. 樣本門檻：**≥600 場**（30 個賽日）才可下結論；n<30 只可 OBSERVE，不得 codify。
4. 任何權重改動需 paired bootstrap（≥1000 次）並報告 95% CI。
5. 消息特徵必須可在 `data_cutoff` 前取得；賽後資料一律禁止。

## 8. KIMI 實作清單（驗收標準）

- [ ] `scripts/intel_score.py`：5 類情報 → `intel_centered` 分數（-1..1），權重可 env 覆寫，含樣本數與證據不足標記。
- [ ] card schema 升至 1.2：支援 `intel_evidence[]` 的 `category` / `observed_at`，沿用 quote 驗證。
- [ ] `predict` 輸出新增 `message_reason` 與 `odds_reference`，並在缺 `message_reason` 時拒絕輸出 pick。
- [ ] `intel-audit` CLI：列出每場每匹馬的情報、quote、來源、計分。
- [ ] 賠率否決邏輯收窄至 §5 兩個條件，並加單元測試。
- [ ] 跑 §7 A/B 回測，提交報告（n、logloss、CI）。
- [ ] 令 `tests/test_intel_first.py` 內所有 `@unittest.skip` 測試轉為 pass。

## 9. 風險

- 情報評分容易變成「講故事」；冇 quote + 來源 + 時間就唔計分。
- 50/30/20 只係起點，未經大樣本驗證；不得對外宣稱有 edge。
- 消息來源若與市場同步（例如賽日報道），會失去優勢；需要獨家或更早的資料。
