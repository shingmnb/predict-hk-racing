# Changelog

所有重要變更會記錄喺呢度。版本採用 Semantic Versioning，日期採用 ISO 格式。

## [Unreleased] - 2026-10-24

### Added

- Race card `schema_version: "1.1"`：意圖證據要有逐字 `quote`，來源要有 `url`，可選 `excerpt`；`quote` 對唔上 `excerpt` 會被拒。
- `scripts/domain.py` 特徵註冊表（`FEATURE_REGISTRY`）：未註冊特徵一律拒絕，杜絕改名賽後特徵漏入模型。
- `fit --min-feature-coverage`：特徵覆蓋率太低（預設 < 0.5）會拒絕訓練。
- 模型版本會保存 `feature_definitions` 快照，方便日後解讀舊模型。
- `race.declared_starters` 同自動 `place_slots` 推導（本地賽 7 匹或以上 3 位、4-6 匹 2 位；少於 4 匹只係 1 位模型近似）。
- `evaluate --kinds` / `--all-kinds`：預設只計 `pre-race` 預測。
- `value-backtest`：用官方派彩結算 walk-forward 注項，支援 flat 同 fractional Kelly（`--cap` 封頂），報 ROI、turnover、drawdown。
- `evaluate --coverage`：按預測信心分層的 selective-use 報告（只睇你會落注嗰部分）。
- `fit --block` / `backtest --ablate`：具名特徵塊與配對 ablation；`promote` 在缺 ablation 時拒絕。
- `promote` 報告加入 `candidates_backtested`，提醒多重比較風險；缺完整配對 ablation 會拒絕，唔提供人手跳過。

### Changed

- **BREAKING** `evaluate` 預設唔再計入 `historical-reconstruction` 預測，報告新增 `scored_forecast_kinds` 同 `excluded_by_forecast_kind`。舊行為可用 `--all-kinds` 還原。
- `place_slots` 可以省略，程式會按 declared starters 自動推導；與 field size 不符只會出 warning。

### Migrated

- 舊 `schema_version: "1.0"` 卡仍然接受，但會提示補 `quote`。建議新卡一律用 `1.1`。

### Added (gate semantics & calibration)

- `calibrate`：固定 source model，只用 `train_before` 之後嘅場次擬合 isotonic/Platt；最後 25% 窗後場次做獨立 ECE holdout，逐場套用校準並記錄 fit／evaluation 樣本與場次，存成新版本。
- `fit --required-block`：特徵塊分 required／optional；`promote` 可累積多個 required blocks 嘅同模型 hash、同窗口／配置、同家族顯著 ablation，並保存 evaluation ids；optional 未通過只記錄，唔否決模型。舊模型保留 latest-only ablation gate。
- 推廣 gate 標註 `information_gain_note`：Δf² 係 log loss 差的單調變換，唔算獨立證據。

### Fixed (calibrated evaluation)

- `backtest`／`value-backtest` 對校準版本逐窗套用儲存的校準層，報告新增 `calibration_applied`／`calibration_through`；champion 比較亦受校準窗防線約束。
- `calibrate` 記錄 `through`（校準用到的日期上限）；測試窗與校準資料重疊時直接報錯並建議 `--test-after`，避免用未來校準器評分過去場次。
- `promote` 拒絕修復前或 `calibration_through` 錯配的校準版回測證據，避免用未校準表現批准校準模型。

## [0.2.0] - 2026-10-24

### Added

- Isotonic（PAVA）同 Platt scaling 機率校準工具，以及 10 等寬 bin ECE 指標；文件明確要求只用 out-of-fold 預測擬合校準器。
- `fit_model` 可選時間順序 validation hold-out 及 early stopping，停止後還原最佳 epoch 係數；預設行為維持不變。
- 校準、ECE、預設兼容性及時間序 hold-out 虛構數據測試。

## [0.1.0] - 2026-10-24

### Added

- 香港賽馬自動資料搜集、來源核實及賽前快照工作流程。
- 去馬意圖證據評分，區分能力、形勢、人馬部署同市場支持。
- 市場去水基準、conditional logit 及 Plackett–Luce 位置概率。
- Append-only SQLite 賽前快照、預測、賽果、模型及評估紀錄。
- Walk-forward 回測、log loss、Brier、校準、bootstrap 及 champion 比較。
- 模型 `fitted`、`promoted`、`rejected` 版本事件與意圖 ablation 流程。
- 虛構數據端到端測試、Skill 打包工具、Issue 與 PR 模板。
- GitHub Actions 測試矩陣，以及 tag 觸發嘅 `.skill`／SHA-256 Release 自動化。

[0.2.0]: https://github.com/shingmnb/predict-hk-racing/releases/tag/v0.2.0
[0.1.0]: https://github.com/shingmnb/predict-hk-racing/releases/tag/v0.1.0
