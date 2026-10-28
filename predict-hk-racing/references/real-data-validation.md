# 真實數據驗證：861 場的結果

呢份文件記錄一次完整的外部驗證：用 HKJC 官方賽果建 861 場語料，重新 fit、backtest、calibrate、value-backtest，睇下 skill 喺真實賽馬數據上到底有冇 edge。

## 1. 建語料

賽果頁（唯一穩定可抓的官方來源）：

```bash
# 逐場抓（例：2026/10/25 沙田 R1）
https://racing.hkjc.com/zh-hk/local/information/archive/localresults?racedate=2026/10/25&racecourse=ST&RaceNo=1
```

- 每場取：名次、馬號、馬名、騎師、練馬師、負磅、檔位、沿途走位、完成時間、獨贏賠率、派彩
- 掃描日期範圍（每週三跑馬地、週末沙田），有資料就係賽日
- 本次：2025-11 至 2026-10，**87 個賽日、861 場、約 10,000 個 runner**

⚠️ 排位表（評分）已冇穩定 HTML／archive 入口；賠率頁係 JS 驅動，純 HTTP 抓唔到（見第 5 節）。

## 2. 由語料自算特徵（無洩漏）

逐場按時間順序處理，只用「該場之前」的賽事：

| 特徵 | 定義 |
|---|---|
| `days_since_run` | 上次出賽距今日數 |
| `recent_finish_mean` | 最近 3 仗名次平均 |
| `career_place_rate` | 生涯前三率 |
| `jockey_win_rate` / `trainer_win_rate` | 累積勝率（≥10 仗才計） |
| `last_trip_early` / `last_trip_close` | **上一仗**首次走位／名次變化（沿途走位） |

注意：同場的走位係賽後資料，**唔可以**做該場特徵，只可以用上一仗。

## 3. 回測結果（訓練 598 場、測試 260 場）

| 模型 | 特徵 | log loss | 市場 | 差 | 95% CI |
|---|---|---:|---:|---:|---|
| `big-v1` | 市場 + 負磅 + 檔位 | 2.0412 | 2.0387 | +0.0025 | [−0.0084, +0.0119] |
| `big-v3` | 市場 + 往績 + 檔位 | 2.0342 | 2.0387 | −0.0045 | [−0.0156, +0.0064] |

**結論：同市場打成平手，冇可測量的 edge。** 260 場測試可以排除大於 0.01 log loss 的優勢。

### 小語料的陷阱

同一批特徵，用 138 場測試時：

| 語料 | 測試場 | `recent_finish_mean` 係數 | `career_place_rate` 係數 |
|---|---:|---:|---:|
| 351 場 | 138 | +0.110 | −0.108 |
| 861 場 | 260 | **+0.062** | **+0.015** |

語料加倍之後，看似有用的係數全部縮向 0 —— **之前嘅「信號」係噪音**。呢個係擴大語料最重要的理由。

## 4. 校準與值博

```
calibrate auto → isotonic（1,271 樣本）
ECE 0.0270 → 0.0185（改善，accepted）

value-backtest kelly 1% cap, edge ≥ 2%（9–10 月）
73 注、turnover 2,060、利潤 −582.5
ROI −28.3%、95% CI [−85%, +36%]、命中率 9.6%
```

**log loss 打平 ≠ 賺錢。** ROI 的 CI 極寬，任何單一 ROI 數字都唔可信。

## 5. CLV 協議

CLV（closing line value）= 我哋入貨價 vs 收市價。噪音比 ROI 細，2–3 個月就睇得出方向。

- 需要**兩個時點**的賠率快照：開售（T-60）同收市（T-5）
- 計法：`CLV = 收市價 / 入貨價 − 1`；長期 mean CLV > 0 才算有 edge
- 判準：正 CLV 比例 > 55%

### 實務限制

HKJC 賠率頁係 JS 驅動，`/zh-hk/local/information/odds` 直接 HTTP 存取會 404；`bet.hkjc.com` 只回 JS 外殼。實務上要：

1. 開賽日早上手動截圖賠率板，或
2. 用瀏覽器自動化（Playwright 等）抓 `bet.hkjc.com` 的渲染後 DOM

歷史賽果頁只有一個賠率（SP），所以**事後無法補算 CLV**，只能前瞻性收集。

## 6. 重現步驟

```bash
# 1) 抓語料（自備抓取器，逐場存 JSON）
# 2) 建 card/result（schema 1.1 + retrieval_after_cutoff: true）
racing_lab.py init --db history.db
racing_lab.py import-card --db history.db --file cards/*.json
racing_lab.py import-result --db history.db --file results/*.json

# 3) fit + 回測（同一測試窗）
racing_lab.py fit --db history.db --model-version v1 \
  --features market_logp,weight_lb,draw --train-before 2026-06-01 --min-races 400
racing_lab.py backtest --db history.db --model-version v1 \
  --min-train-races 400 --min-test-races 200 --test-after 2026-06-01 --bootstrap 500

# 4) 校準與值博
racing_lab.py calibrate --db history.db --model-version v1 --new-version v1-cal --method auto
racing_lab.py value-backtest --db history.db --model-version v1 --stake kelly --cap 0.01
```

## 7. 對設計的含意

1. **市場基準係主角**：任何模型先要贏去水市場，否則只係重新包裝。
2. **公開基本面冇 alpha**：負磅、檔位、往績都已被市場吸收；要非市場優勢，必須靠**獨家／及時**的情報（配備、騎師選鞍、試閘、馬房原句）。
3. **樣本比特徵重要**：138 場的 CI 寬到 ±0.028，任何 ±0.02 的「優勢」都係噪音。
4. **值博要獨立驗證**：log loss 打平唔代表賺錢，必須用 ROI／CLV 交叉確認。
