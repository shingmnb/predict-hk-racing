# 資料格式與命令

## 目錄
- 檔案與狀態
- Race card
- Result
- 完整命令流程
- 模型與紀錄

## 檔案與狀態

將用戶資料放當前工作區，唔好寫入 managed skill 目錄：

```text
racing-data/
├── racing.db
├── cards/          # 每次賽前／臨場快照
├── results/        # 官方賽果快照
├── predictions/    # 已凍結概率
├── models/         # 候選模型配置
├── evaluations/    # 評估及回測報告
└── sources/        # 可合法保存嘅原始資料
```

複製 `assets/race-card-template.json` 及 `assets/race-result-template.json` 開始。以下命令中的 `<SKILL>` 指本 Skill 根目錄；所有腳本只用 Python 標準庫。

## Race card

必填結構：

```json
{
  "schema_version": "1.0",
  "collection_mode": "live",
  "captured_at": "2026-10-24T20:00:00+08:00",
  "data_cutoff": "2026-10-24T20:00:00+08:00",
  "race": {
    "race_id": "2026-10-25-ST-R1",
    "meeting_date": "2026-10-25",
    "venue": "ST",
    "race_no": 1,
    "off_time": "2026-10-25T13:00:00+08:00",
    "distance_m": 1200,
    "declared_starters": 12,
    "place_slots": 3
  },
  "sources": [{"source_id": "S1", "title": "官方排位表", "url": "...", "excerpt": "原文節錄"}],
  "runners": []
}
```

每匹 runner 必填 `horse_no`、`horse_name`、`scratched`；建議加入 `horse_id`、`jockey`、`trainer`、`draw`、`weight_lb`、`rating`、`win_odds`、`place_odds`。

`features` 只放數值。模型可直接用 `features` 內欄名，亦可用 runner 頂層 `rating`、`draw`、`weight_lb`。兩個自動特徵：

- `market_logp`：完整全場獨贏賠率計出去水概率，再取 log。
- `intent_centered`：由 `intent_evidence` 計出，範圍約 −1 至 +1。

每項意圖證據格式：

```json
{
  "signal": "declared_gear_change",
  "direction": "positive",
  "strength": 0.3,
  "confidence": 0.7,
  "source_id": "S1",
  "quote": "是日配備改動：加戴眼罩",
  "note": "觀察事實加限制；推論同事實要分開寫"
}
```

`direction` 可用 `positive`、`negative`、`neutral` 或 1、−1、0。`strength` 係訊號強度，`confidence` 係來源及觀察可靠度，兩者都係 0–1。`source_id` 必須存在於 `sources`。

Schema 1.1 起，每項證據亦要 `quote`：逐字摘錄來源原文（正規化後至少 8 字）。如果 `source` 有 `excerpt`，`quote` 必須真係出現喺節錄之內，否則 `import-card` 會拒絕。`excerpt` 同 `quote` 係防幻覺嘅核心，唔好自己改寫。

`features` 只准用 `scripts/domain.py` 註冊表入面嘅名稱；未註冊特徵會被拒絕（真要例外就用 `--allow-unknown-features`，等於自己承擔滲漏風險）。

## Result

```json
{
  "schema_version": "1.0",
  "race_id": "2026-10-25-ST-R1",
  "recorded_at": "2026-10-25T14:00:00+08:00",
  "sources": [{"source_id": "R1", "title": "官方賽果", "url": "..."}],
  "results": [
    {"horse_no": 2, "finish_position": 1, "status": "OK"},
    {"horse_no": 1, "finish_position": 2, "status": "OK"}
  ]
}
```

需要評估位置概率時，提供全部 active runners 名次。派彩可保留作另作投注審計，但唔會進入賽前特徵。

## 完整命令流程

初始化及新增賽前快照：

```bash
python "<SKILL>/scripts/racing_lab.py" init --db "racing-data/racing.db"
python "<SKILL>/scripts/racing_lab.py" import-card --db "racing-data/racing.db" --file "racing-data/cards/race.json"
```

凍結預測（`auto` 用最新 promoted，否則市場基準）：

```bash
python "<SKILL>/scripts/racing_lab.py" predict \
  --db "racing-data/racing.db" \
  --race-id "2026-10-25-ST-R1" \
  --model-version auto \
  --output "racing-data/predictions/2026-10-25-ST-R1.json"
```

臨場更新：先新增 card snapshot，再跑一次 `predict`。每個 `run_id` 都保留。歷史重建必須用 card 嘅 `collection_mode: historical`；已有賽果後程式會拒絕補做假賽前預測，真正歷史評估走 `backtest`。

賽後結算及評估：

```bash
python "<SKILL>/scripts/racing_lab.py" import-result --db "racing-data/racing.db" --file "racing-data/results/race.json"
python "<SKILL>/scripts/racing_lab.py" evaluate --db "racing-data/racing.db" --output "racing-data/evaluations/latest.json"
```

`evaluate` 預設只計 `pre-race` 預測，報告會列出 `scored_forecast_kinds` 同 `excluded_by_forecast_kind`。要覆盤歷史重建，用 `--kinds historical-reconstruction` 分開出報告；`--all-kinds` 只作 legacy 用途，唔建議用嚟計 KPI。

訓練新候選版本：

```bash
python "<SKILL>/scripts/racing_lab.py" fit \
  --db "racing-data/racing.db" \
  --model-version "candidate-2026-10-24" \
  --features "market_logp,intent_centered,rating,draw,weight_lb,cd_place_rate,recent_finish_mean,days_since_run" \
  --train-before "2026-10-24" \
  --min-races 30 \
  --min-feature-coverage 0.5 \
  --output "racing-data/models/candidate-2026-10-24.json"
```

向前滾動回測及升級：

```bash
python "<SKILL>/scripts/racing_lab.py" backtest \
  --db "racing-data/racing.db" \
  --model-version "candidate-2026-10-24" \
  --min-train-races 30 \
  --min-test-races 50 \
  --refit-every 5 \
  --bootstrap 1000 \
  --output "racing-data/evaluations/backtest-candidate-2026-10-24.json"
python "<SKILL>/scripts/racing_lab.py" promote --db "racing-data/racing.db" --model-version "candidate-2026-10-24"
```

`promote` 唔允許人手跳過門檻；未合資格會寫 `rejected`。做 ablation 唔需要另開版本：`fit` 用 `--block name=feature,feature` 宣告特徵塊，`backtest --ablate name` 會在同一測試窗口重跑一次（移除該塊）並出 paired bootstrap。每份報告保存 `source_model_hash`；required 證據必須完整、顯著而且同最新回測上下文一致，唔提供人手覆寫。報告列 `candidates_backtested`／`candidates_in_family`，promote 強制以 Bonferroni `effective_alpha = 0.05/n` 計調整後 CI 入閘（唔再只係提醒）；候選家族改變會標 `multiple_comparison_stale`，要先重跑 backtest 先可以再 promote。若入閘 comparison 嘅 bootstrap 樣本不足以解析 adjusted CI 下尾，報告會將 adjusted `ci` 留空、記錄 `ci_warning`／`bootstrap_resolution_warning` 並拒絕 promote；要增加 `--bootstrap` 或按預先登記準則收窄家族後重跑，唔會將 bootstrap 最小值當分位數。

## 特徵塊：required vs optional

- `fit --block name=feature,feature`：宣告一個**可選**特徵塊；
- `fit --block ... --required-block name`：標記為**必要**；required 名稱冇對應 `--block` 會直接報錯；
- `backtest --ablate name`：逐個 block 做配對 ablation（paired bootstrap）；
- 每個 **required** block 都要各自通過；`promote` 只累積同一 source model hash、方法／窗口、champion、candidate／baseline／逐場數據、候選家族同 alpha 嘅 `evaluation_runs`，並喺 `required_block_ablation_evidence` 記錄 evaluation id。呢個係 per-block necessity，唔代表 joint necessity；
- optional block 若未通過，只記入 `optional_blocks_unproven`，唔會否決模型或佔用入閘 bootstrap resolution；程式唔會自動剔除，合理做法係另 fit 一個移除該塊嘅新版本；
- `required_block_ablation_missing` 非空時，promote 一定拒絕。舊模型冇 `required_blocks` key 時，仍要求 latest backtest 有顯著 ablation，唔會跨 run 累積。

## 校準命令

```bash
python "<SKILL>/scripts/racing_lab.py" calibrate \
  --db "racing-data/racing.db" \
  --model-version "candidate-2026-10-24" \
  --new-version "candidate-2026-10-24-cal" \
  --method isotonic --min-train-races 50 --min-samples 200
```

- 固定未校準嘅 source model，只用 `meeting_date >= train_before` 嘅已完成場次預測，再 fit isotonic 或 Platt；唔會 refit fold model，亦唔會退回訓練窗；
- 窗後最後 25% 場次（向上取整、至少一場）係獨立 evaluation holdout，較早場次先係 fit points；`--min-samples` 檢查 fit points，`samples` 係 fit runner 數，`evaluation_samples` 係 holdout runner 數，`post_train_samples` 係兩者合計；
- `ece_before`／`ece_after` 只用同一批較後 holdout；after 會逐場套用校準、全場重新 normalise，唔會逐匹單點套用或重用 fit points；
- config 記錄 fit／evaluation race IDs、場數、日期範圍同 `train_before` 分界；結果存成**新版本**（append-only，唔改舊 model）；
- `predict --model-version <new>` 會自動逐場套用、重新 normalise，輸出 `calibration` 欄位同樣本來源；
- 窗後場次或 fit 樣本不足會直接報錯並話你知欠幾多，唔會用訓練窗 in-sample 預測偷偷擬合。

> **校準版只可以在校準資料之後的場次評估。** `backtest`／`value-backtest` 會讀
> `calibration.through`；如果測試窗第一場 ≤ through，會直接報錯並建議 `--test-after`。
> 報告會寫 `calibration_applied` 同 `calibration_through`。因為校準用「現在為止所有
> post-training 場次」，校準完要等新賽事入庫才有合法回測窗口；長遠應做「逐窗前移校準」。

## 價值與選擇性使用

```bash
python "<SKILL>/scripts/racing_lab.py" value-backtest \
  --db "racing-data/racing.db" \
  --model-version "candidate-2026-10-24" \
  --stake kelly --cap 0.01 --bankroll 10000 \
  --kelly-multiplier 0.25 --max-race-fraction 0.05 \
  --test-after "2026-10-24" \
  --output "racing-data/evaluations/value.json"
```

- `--stake flat|kelly`：flat 沿用固定比例、最大 edge 單注；kelly 統一用 `scripts/staking.py` 嘅 fractional Kelly，--cap 限制單注，--max-race-fraction 限制同場總曝險，--stake-unit 控制向下取整。
- 可另輸入 `--pool-size` 同 `--max-pool-fraction`（預設 0.001）限制彩池衝擊；pool size 係回測假設，會寫入報告，唔係自動觀測嘅歷史數據。
- 結算優先用官方 `win_dividend`（香港以每 HK$10 報價，程式除以 10），冇先用快照賠率。
- 報 turnover、ROI、strike rate、max drawdown、期末 bankroll；Kelly 會放大概率估計誤差，ROI 必須由時間外結算證明，唔好預設正回報。
- `evaluate --coverage 5,10,20,50` 會按預測信心分層，報每層 log loss／Brier／命中率（selective-use），用嚟答「只買邊啲場」。

## 位置名額與特徵註冊

`place_slots` 可以省略：程式會用 `declared_starters`（冇填就用 active runners）按本地賽規則推導——7 匹或以上 3 位、4-6 匹 2 位。少於 4 匹時嘅 1 位只係模型近似，唔代表有正式 Place pool。若果你寫死一個同 field size 唔相符嘅值，`import-card` 會出 warning 並照用你嘅值（例如有馬退出但 declared starters 未變）。

特徵註冊表喺 `scripts/domain.py`：每個特徵要寫低單位、來源同定義。加新特徵就喺 `SPECS` 加一項，順手喺 `references/modeling-and-backtesting.md` 講清楚點樣保證佢喺 `data_cutoff` 前已存在。

## 模型與紀錄

SQLite 保存 race card、result、prediction、model、model event、evaluation。主要紀錄表禁止 update/delete；修正方法係新增快照或版本。

- `card_snapshots`：同一場不同資料截止時間。
- `prediction_runs`：某快照、某模型嘅不可變概率。
- `result_snapshots`：官方結果及更正。
- `models`：特徵、係數、標準化、填補值、訓練窗口、hyperparameters。
- `model_events`：fitted、promoted、rejected。
- `evaluation_runs`：正式預測評估及 walk-forward 報告。

預設 30 場只係可訓練下限、50 場只係可評估下限，唔代表統計上一定足夠。真實使用應按賽事類型、特徵數及漂移風險增加樣本；測試小樣本只可用嚟驗證程式，唔好用嚟宣稱模型有效。
