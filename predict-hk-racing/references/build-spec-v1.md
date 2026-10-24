# 三 Agent 共建規格 v1（KIMI 實作版）

> 角色：Hermes／其他 agent 出思路、設計、審查；**KIMI 負責寫 code 同 tests**。
> 原則：一個 PR 一個模組、先寫測試、唔改已合併介面（只可加 optional 欄位）。

## 1. 分層與邊界

| 層 | 職責 | 現有模組 | 待建模組 |
|---|---|---|---|
| L1 數據／執行 | 賠率攝取、快照、賽日流程 | `import-odds`、`diagnose-card`（PR #9） | `tickets.py` |
| L2 模型／校準 | 概率、pair、校準、gate | Shin、`pair_probabilities`、isotonic/Platt、Δf²（#2/#3/#6） | — |
| L3 評估／風控 | CLV、注碼、漂移、審計 | `staking.py`（#8）、value backtest（#7） | `clv.py`、`drift.py`、`snapshot_audit.py` |

## 2. 共用資料契約（v1.1，只加 optional 欄位）

**Card** 新增：

```json
"odds_snapshot": {
  "captured_at": "2026-10-24T10:30:00+08:00",
  "source_id": "HKJC-WP",
  "minutes_to_off": 10.0,
  "quoted_runners": 11
}
```

**Prediction**（`predict` 輸出）必須有：

```
race_id, model_version, algorithm, run_id, data_cutoff,
market_complete(bool), market_completeness(0–1),
runners[].win_probability, runners[].place_probability
```

**Odds file**（`import-odds` 輸入）：

```json
{"race_id": "...", "captured_at": "...", "source": {...},
 "odds": [{"horse_no": 2, "win_odds": 2.4, "place_odds": 1.5}]}
```

**Closing odds file**：同上，加 `"kind": "closing"`。

## 3. 待實作模組

### M1 `tickets.py` — Q／PQ 票務建構（P0）

```python
def build_tickets(
    prediction: dict,
    odds: dict,                     # {"q": {(i,j): 12.5}, "pq": {(i,j): 4.2}}，可缺
    bankroll: float,
    *,
    min_edge: float = 0.02,
    takeout: float = 0.175,         # 只在「估算賠率」模式用
    max_combos_q: int = 4,
    max_combos_pq: int = 4,
    kelly_multiplier: float = 0.25,
    unit: float = 10.0,
) -> dict
```

**規則**

1. 用 `racing_model.pair_probabilities(win_probs, slots=2|3)` 取聯合機率。
2. **有真實 Q/PQ 賠率**：`ev = p * odds - 1`（賠率已含抽水，唔好再乘 0.825）。
3. **只有估算**：`fair_odds = 1/p`，`ev = None`，並在輸出標 `"estimated": true`；唔准假裝有 EV。
4. 選票：PQ 取 `p_pq` 最高且 `ev >= min_edge`（若有）；Q 同理；最多 `max_combos_*` 注。
5. 每注 stake 由 `staking.stake_size()` 計，再用 `staking.plan_race()` 套全場上限。
6. 輸出：

```json
{"banker": 3, "q": [{"legs": [3,10], "p": 0.078, "fair_odds": 12.8,
   "market_odds": 13.5, "ev": 0.053, "stake": 20}],
 "pq": [...], "total_stake": 180, "estimated": false}
```

**驗收測試**

- Q 所有 pair 機率和 = 1；PQ `Σ_j P(i,j) = 2 × P(i 入三甲)`。
- 注碼 ≤ bankroll / pool / race cap，且為 `unit` 的倍數。
- 冇 `market_odds` 時 `ev is None` 且 `estimated is true`。
- 同一輸入兩次結果一致（決定性）。

### M2 `clv.py` — 收市線價值（P1）

```python
def clv_report(predictions: list[dict], closing_odds: dict[str, dict]) -> dict
```

- 每注：`clv = p_model * closing_odds - 1`（只計有 closing 的注）。
- 輸出：`mean_clv`、`median_clv`、`pct_positive`、`by_odds_bucket`（1–2／2–4／4–8／8+）、`coverage`。
- 缺 closing 的注要計入 `skipped`，唔可以靜默當 0。

**驗收**：固定 fixture 數值；缺價 → `skipped` +1；coverage = matched / total。

### M3 `drift.py` — 漂移監測（P2）

```python
def drift_report(rows: list[dict], *, window: int = 30, psi_bins: int = 10) -> dict
```

- 滾動 `logloss`、按賠率分桶的 calibration gap、數值特徵 PSI。
- flag：`logloss_delta > 0.02`、`psi > 0.2`、`calibration_gap > 0.05`。
- 輸出每期指標 + `alerts` 陣列（`{"kind","severity","detail"}`）。

**驗收**：人工製造 shift 要觸發；穩定序列唔可以誤報。

### M4 `snapshot_audit.py` — 審計鏈（P3）

```python
def audit_snapshots(db_path: str, race_id: str | None = None) -> dict
```

- 檢查：每個 race 的 snapshot 數目、`payload_hash` 唯一、`captured_at` 單調、`minutes_to_off` 遞減、有無 result 之後的 card。
- CLI：`audit-snapshots --db FILE [--race-id R]`。
- 發現異常要 `ok=false` 並列證據（snapshot_id + 原因）。

**驗收**：正常 DB `ok=true`；手動插入違規 snapshot → 準確指出。

## 4. 不變量（Kimi 必須寫成測試）

1. **Append-only**：任何 UPDATE/DELETE 觸發即失敗。
2. **無泄漏**：`data_cutoff <= off_time`；有 result 之後拒收 card／odds。
3. **校準只可用 OOF**：`isotonic_fit` / `platt_fit` 的 docstring 要保留警告，測試要驗證 in-sample 會被拒絕（加 `require_oof=True` 參數）。
4. **Promotion gate**：`Δf² > 0`、logloss CI 上界 < 0、Brier 不差過市場、贏 champion，缺一不可。
5. **注碼**：永不超过 bankroll／單注／彩池／同場上限，且向下取整。
6. **Q/PQ 機率**：兩條不變量（總和 = 1；邊際 = 2×place）。

## 5. 交付順序

| 階段 | 模組 | 依賴 |
|---|---|---|
| P0 | M1 tickets | `pair_probabilities`、`staking` |
| P1 | M2 clv | `import-odds`（closing snapshot） |
| P2 | M3 drift | evaluate / backtest rows |
| P3 | M4 audit | 現有 schema |

每個 PR 必須：code + tests + doc + CLI（如適用），**唔可以混合兩個模組**。

## 6. 開放問題（要另外兩個 agent 決定）

1. Q/PQ 真實 pool 賠率有無穩定公開來源？冇就永遠 `estimated=true`。
2. 要唔要支援 place／show pool 的 EV（Ziemba：place 池 bias 更極端）？
3. 第三個 agent 負責 L1／L2／L3 邊一層？
4. 新 skill 最終落 Hermes `skills/` 定留在 repo？（影響 cron 同 import 路徑）

## 7. Definition of Done

- [ ] `python -m unittest discover -s tests` 全綠，CI 3.11／3.12 綠
- [ ] 純 stdlib、零新依賴
- [ ] doc 更新（`references/`）
- [ ] 一個 PR 一個模組
- [ ] 新增不變量測試，唔可以只測 happy path
