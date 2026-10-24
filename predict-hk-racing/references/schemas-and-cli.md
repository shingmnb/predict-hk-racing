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
    "place_slots": 3
  },
  "sources": [{"source_id": "S1", "title": "官方排位表", "url": "..."}],
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
  "note": "逐字或忠實摘錄可觀察事實，並註明限制"
}
```

`direction` 可用 `positive`、`negative`、`neutral` 或 1、−1、0。`strength` 係訊號強度，`confidence` 係來源及觀察可靠度，兩者都係 0–1。`source_id` 必須存在於 `sources`。

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

訓練新候選版本：

```bash
python "<SKILL>/scripts/racing_lab.py" fit \
  --db "racing-data/racing.db" \
  --model-version "candidate-2026-10-24" \
  --features "market_logp,intent_centered,rating,draw,weight_lb,cd_place_rate,recent_finish_mean,days_since_run" \
  --train-before "2026-10-24" \
  --min-races 30 \
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

`promote` 唔允許人手跳過門檻；未合資格會寫 `rejected`。做意圖 ablation 時，用另一個新版本名稱及完全相同的訓練／測試窗口，只移除 `intent_centered`。

## 模型與紀錄

SQLite 保存 race card、result、prediction、model、model event、evaluation。主要紀錄表禁止 update/delete；修正方法係新增快照或版本。

- `card_snapshots`：同一場不同資料截止時間。
- `prediction_runs`：某快照、某模型嘅不可變概率。
- `result_snapshots`：官方結果及更正。
- `models`：特徵、係數、標準化、填補值、訓練窗口、hyperparameters。
- `model_events`：fitted、promoted、rejected。
- `evaluation_runs`：正式預測評估及 walk-forward 報告。

預設 30 場只係可訓練下限、50 場只係可評估下限，唔代表統計上一定足夠。真實使用應按賽事類型、特徵數及漂移風險增加樣本；測試小樣本只可用嚟驗證程式，唔好用嚟宣稱模型有效。
