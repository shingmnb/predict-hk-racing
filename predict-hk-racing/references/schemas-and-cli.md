# 資料格式與命令

## 檔案與狀態

將用戶資料放當前工作區，唔好寫入 managed skill 目錄：

```text
racing-data/
├── racing.db
├── cards/          # 每次賽前／臨場快照
├── analyses/       # 已凍結情報分析（核心輸出）
├── results/        # 官方賽果快照
├── predictions/    # 已驗證模型嘅概率輸出（可選）
├── models/         # 候選模型配置
├── evaluations/    # 評估及回測報告
└── sources/        # 可合法保存嘅原始資料
```

複製 `assets/race-card-template.json` 及 `assets/race-result-template.json` 開始。`<SKILL>` 指本 Skill 根目錄；所有腳本只用 Python 標準庫。

## Race card

必填結構：

```json
{
  "schema_version": "1.1",
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

每匹 runner 必填 `horse_no`、`horse_name`、`scratched`；建議加入 `horse_id`、`jockey`、`trainer`、`draw`、`weight_lb`、`rating`。`win_odds`、`place_odds` 係可選原始市場觀察，只保存當時數值，唔會轉成概率或模型特徵。

`features` 只放數值。模型可直接用 `features` 內欄名，亦可用 runner 頂層 `rating`、`draw`、`weight_lb`。自動特徵只有：

- `intent_centered`：由 `intent_evidence` 計出，範圍約 −1 至 +1；係候選情報特徵，唔係勝率。

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

`direction` 可用 `positive`、`negative`、`neutral` 或 1、−1、0。`strength` 同 `confidence` 都係 0–1，屬分析標籤而唔係已驗證權重。`source_id` 必須存在於 `sources`。

Schema 1.1 起，每項證據要 `quote`：逐字摘錄來源原文（正規化後至少 8 字）。如果 `source` 有 `excerpt`，`quote` 必須真係出現喺節錄之內，否則 `import-card` 會拒絕。`excerpt` 同 `quote` 係防幻覺核心，唔好自己改寫。

`features` 只准用 `scripts/domain.py` 註冊表嘅名稱；未註冊特徵會被拒絕。例外旗標 `--allow-unknown-features` 會關閉呢道防線，使用時要自己承擔滲漏風險。

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

需要評估位置概率時，提供全部 active runners 名次。派彩可保留作投注審計，但唔會進入賽前特徵。

## 完整賽日時點資料鏈

`meeting_chain.py` 用一份 build spec 重建整個賽日：保留排位 PDF、逐頁派生文本、官方賽果 HTML、來源登記及 SHA-256，產生逐場歷史卡、賽果、manifest、驗收報告同獨立 append-only SQLite。呢個構建器額外需要 `beautifulsoup4` 同 `pdfplumber`；`racing_lab.py` 核心命令仍只用 Python 標準庫。

```bash
python "<SKILL>/scripts/meeting_chain.py" build \
  --spec "racing-data/meetings/<date>-<venue>/build-spec.json" \
  --workspace "."
python "<SKILL>/scripts/meeting_chain.py" validate \
  --meeting-dir "racing-data/meetings/<date>-<venue>"
```

Build spec 要分開記內容時點、截止前可用時間、實際補採時間同資料截止時間，並附可用性證明。所有新卡必須係 `collection_mode: historical`、唔進入 forward KPI；呢條工程鏈唔會產生預測或模型。賽果頁最終獨贏賠率只存做 `final_win_odds_reference`，標為 `historical_closing_reference`，唔會寫入賽前卡。`engineering_status: passed` 只代表結構、來源雜湊、覆蓋同防洩漏檢查通過；如果缺少可驗證嘅同時點全場賽前市場快照，P0 仍要報 `incomplete_market_history`。

## 核心流程：匯入並凍結情報分析

```bash
python "<SKILL>/scripts/racing_lab.py" init --db "racing-data/racing.db"
python "<SKILL>/scripts/racing_lab.py" import-card \
  --db "racing-data/racing.db" --file "racing-data/cards/race.json"
python "<SKILL>/scripts/racing_lab.py" analyze \
  --db "racing-data/racing.db" --race-id "2026-10-25-ST-R1" \
  --output "racing-data/analyses/2026-10-25-ST-R1.json"
```

`analyze` 輸出 `kind: race-intelligence-analysis-v1`，逐匹包括：

- `observed_features`、`deployment_support`；
- `supporting_evidence`、`opposing_evidence`、`neutral_evidence`；
- 每項證據嘅 `quote`、來源標題、URL 同擷取時間；
- `unknowns`：冇證據或冇反證時會明確標記；
- 可選 `win_odds_snapshot`／`place_odds_snapshot` 原始數值。

輸出**冇** `win_probability`、`place_probability` 或公平倍數。部署支持度係未校準嘅證據整理，唔可以當勝率或代入注碼公式。

臨場更新要先新增 card snapshot，再跑一次 `analyze`。每個 `run_id` 都保留。歷史重建必須用 `collection_mode: historical`；已有賽果後程式會拒絕補做假賽前分析。

## 10 倍以上半冷決策

半冷紀錄使用獨立目錄，唔同概率模型預測混合：

```text
racing-data/longshot/
├── cards/       # <race_id>.json：T-2h 目標全場 WIN 快照
├── decisions/   # <race_id>.json：選擇、拒絕或棄選
├── results/     # <race_id>.json：官方賽果及派彩
└── evaluations/
```

複製 `assets/longshot-decision-template.json`。核心欄位係 `rule_version`、`race_id`、`created_at`、先於快照嘅 `fundamental_created_at`／`fundamental_run_id`、`market_blind`、`snapshot_policy`、`sources` 及覆蓋全部合資格馬嘅 `reviews`。入選 review 要有兩個證據 `family`、至少一項非 `market`、逐字 `quote`、`strongest_counterevidence`、非空 `unknowns` 及 `withdraw_if`；拒絕要有 `reason`，零選擇要有 `abstain_reason`。完整規則見 [longshot.md](longshot.md)。

```bash
python "<SKILL>/scripts/longshot.py" validate \
  --card "racing-data/longshot/cards/<race_id>.json" \
  --decision "racing-data/longshot/decisions/<race_id>.json" \
  --output "racing-data/longshot/evaluations/<race_id>-validation.json"
python "<SKILL>/scripts/longshot.py" backtest \
  --input-dir "racing-data/longshot" --bootstrap 1000 \
  --output "racing-data/longshot/evaluations/backtest.json"
```

`validate` 只驗證時序、全場審查及證據結構；`forward_kpi_eligible: true` 先可納入正式回測。非合格快照仍可保存棄選紀錄，但唔會混入 KPI。

## 非熱門核心 Q／位置 Q

已通過單馬驗證嘅核心先可以建立 `tickets/<race_id>.json`。組合票用 content hash 綁定單馬決策與市場卡，保存兩名研究員及獨立核查員嘅程序隔離、逐對證據、形勢機制、反證及全部注額；零組合必須棄選。

```bash
python "<SKILL>/scripts/exotic.py" freeze \
  --card "racing-data/longshot/cards/<race_id>.json" \
  --longshot-decision "racing-data/longshot/decisions/<race_id>.json" \
  --ticket "racing-data/longshot/tickets/<race_id>.json" \
  --output "racing-data/longshot/evaluations/<race_id>-freeze.json"
python "<SKILL>/scripts/exotic.py" settle \
  --card "racing-data/longshot/cards/<race_id>.json" \
  --longshot-decision "racing-data/longshot/decisions/<race_id>.json" \
  --ticket "racing-data/longshot/tickets/<race_id>.json" \
  --result "racing-data/longshot/results/<race_id>.json" \
  --freeze "racing-data/longshot/evaluations/<race_id>-freeze.json" \
  --output "racing-data/longshot/evaluations/<race_id>-settlement.json"
```

Result 內 `exotic_dividends.Q`／`PQ` 保存官方逐組 `horses`、每 HK$10 `dividend_hkd` 及明確退款。`meeting_chain.py` 會由官方結果 HTML 擷取呢個區塊。結算分開核心入位、目標馬對、官方派彩及現金帳；非核心派彩照計現金，唔計目標命中。格式、代理欄位及例外處理見 [exotic.md](exotic.md)。

## 賽後記錄與評估

```bash
python "<SKILL>/scripts/racing_lab.py" import-result \
  --db "racing-data/racing.db" --file "racing-data/results/race.json"
python "<SKILL>/scripts/racing_lab.py" evaluate \
  --db "racing-data/racing.db" --output "racing-data/evaluations/latest.json"
```

`evaluate` 只評分有完整概率欄嘅模型預測，會跳過 `analyze` 紀錄。預設只計 `pre-race`，報告列出 `scored_forecast_kinds` 同 `excluded_by_forecast_kind`；歷史重建用 `--kinds historical-reconstruction` 分開報告。`--all-kinds` 只作 legacy 用途。

情報本身嘅賽後核查係逐項檢查事實、反證、形勢假設及未知，唔好用賽果將意圖標成「證實／推翻」。

## 可選模型訓練與概率輸出

```bash
python "<SKILL>/scripts/racing_lab.py" fit \
  --db "racing-data/racing.db" \
  --model-version "candidate-2026-10-24" \
  --features "intent_centered,rating,draw,weight_lb,cd_place_rate,recent_finish_mean,days_since_run" \
  --train-before "2026-10-24" \
  --min-races 30 --min-feature-coverage 0.5 \
  --output "racing-data/models/candidate-2026-10-24.json"
```

只有明確模型先可以 `predict`：

```bash
python "<SKILL>/scripts/racing_lab.py" predict \
  --db "racing-data/racing.db" --race-id "2026-10-25-ST-R1" \
  --model-version auto --output "racing-data/predictions/2026-10-25-ST-R1.json"
```

`auto` 只用最新 promoted；冇 promoted 會報錯並提示使用 `analyze`。`baseline`、`uniform-baseline` 或其他內部對照唔係合法賽日模型。模型輸出概率同原始賠率快照，但唔附公平倍數。

## 向前回測、消融及升級

```bash
python "<SKILL>/scripts/racing_lab.py" backtest \
  --db "racing-data/racing.db" --model-version "candidate-2026-10-24" \
  --min-train-races 30 --min-test-races 50 --refit-every 5 --bootstrap 1000 \
  --output "racing-data/evaluations/backtest-candidate-2026-10-24.json"
python "<SKILL>/scripts/racing_lab.py" promote \
  --db "racing-data/racing.db" --model-version "candidate-2026-10-24"
```

`backtest` 用均勻無資訊基準作內部對照，報告欄位係 `uniform_baseline`、`information_gain_vs_uniform`，gate 包括 `logloss_beats_uniform_baseline` 同 `brier_not_worse_than_uniform`。基準唔會出現喺賽日 `predict`。

`fit --block name=feature,feature` 宣告特徵塊，`backtest --ablate name` 喺同一測試窗口移除並做 paired bootstrap。`--required-block name` 要求該塊證明時間外增量；optional 未通過只記 `optional_blocks_unproven`。

`promote` 唔允許人手跳過門檻；未合資格會寫 `rejected`。候選家族以 Bonferroni 調整，家族改變會標 `multiple_comparison_stale`；bootstrap 解析度不足時 adjusted CI 留空並拒絕升級。詳見 [modeling-and-backtesting.md](modeling-and-backtesting.md)。

## 校準命令

```bash
python "<SKILL>/scripts/racing_lab.py" calibrate \
  --db "racing-data/racing.db" --model-version "candidate-2026-10-24" \
  --new-version "candidate-2026-10-24-cal" --method auto \
  --min-train-races 50 --min-samples 200
```

- 固定未校準 source model，只用 `meeting_date >= train_before` 嘅場次；最後 25% 係獨立 evaluation holdout。
- `ece_before`／`ece_after` 只用同一批較後 holdout；套用時逐場全場重新 normalise。
- `--method auto`：fit points ≥ 500 用 isotonic，否則用 Platt。
- holdout ECE 變差預設拒絕；`--allow-worse-calibration` 先會照存。
- 校準輸出 clamp 到 `[0.001, 0.99]` 再 normalise。
- 校準版只可以在 `calibration.through` 之後嘅場次評估；`promote` 會核對 source hash、calibration applied 同 through。

## 校準漂移監控

```bash
python "<SKILL>/scripts/racing_lab.py" calibration-drift \
  --db "racing-data/racing.db" --model-version all --period month --min-samples 100
```

- 按 `meeting_date` 分月（或 `--period meeting` 逐賽日）計 ECE；
- 基準係模型的 `calibration.ece_after`（冇就用 `none`，只報 ECE）；
- `samples < --min-samples` 的期間只作參考，不會標記 drift；
- `delta > --threshold`（預設 0.02）就標 `drift: true`，並列出 `drifted_periods`；
- 報告存入 `evaluation_runs`（kind `calibration-drift`）；
- 情報分析冇概率，唔會產生校準點。

## 可選價值回測

```bash
python "<SKILL>/scripts/racing_lab.py" value-backtest \
  --db "racing-data/racing.db" --model-version "candidate-2026-10-24" \
  --stake kelly --cap 0.01 --bankroll 10000 \
  --kelly-multiplier 0.25 --max-race-fraction 0.05 \
  --test-after "2026-10-24" --output "racing-data/evaluations/value.json"
```

只接受實際模型版本，唔接受內部基準。結算優先用官方 `win_dividend`（香港以每 HK$10 報價，程式除以 10），冇先用快照賠率。報 turnover、ROI、strike rate、max drawdown、期末 bankroll、ROI 區間、單注曝險及盈利集中度；唔好預設正回報。

`settlement` 另外報三個風險指標：

- `roi_ci95`：按場次 bootstrap（1,000 次）的 ROI 95% 區間；
- `max_single_stake_fraction`：單注最大佔當時 bankroll 比例；
- `profit_concentration`：最佳一場佔總利潤比例（越接近 1 表示盈利越集中、越不可靠）。

`evaluate --coverage 5,10,20,50` 可按模型最高概率分層報告 selective-use，唔適用於情報支持度。

## 位置名額與歷史重建

`place_slots` 可以省略：程式用 `declared_starters`（冇填就用 active runners）推導——7 匹或以上 3 位、4-6 匹 2 位。少於 4 匹嘅 1 位只係模型近似，唔代表有正式 Place pool。

如果卡片由官方賽果頁事後重建、來源喺 `data_cutoff` 後先取得，可加 `retrieval_after_cutoff: true` 並保持 `collection_mode: historical`。程式會警告，但呢類卡永遠唔可以當賽前快照；`live` 卡使用會被拒絕。

## 模型與紀錄

SQLite 保存 race card、result、analysis／prediction、model、model event、evaluation。主要紀錄表禁止 update/delete；修正方法係新增快照或版本。

- `card_snapshots`：同一場不同資料截止時間。
- `prediction_runs`：保存不可變情報分析或模型概率；用 `kind`／`algorithm` 區分，評估只讀概率輸出。
- `result_snapshots`：官方結果及更正。
- `models`：特徵、係數、標準化、填補值、訓練窗口、hyperparameters。
- `model_events`：fitted、promoted、rejected。
- `evaluation_runs`：正式預測評估及 walk-forward 報告。

預設 30 場只係可訓練下限、50 場只係可評估下限，唔代表統計上一定足夠。測試小樣本只可用嚟驗證程式，唔好用嚟宣稱模型有效。
