# Obsidian 歷史數據

Obsidian 做「人手寫／人可讀」的前端，`racing-data/` 的 JSON 做「機器格式」，SQLite 做 append-only 記錄。三者之間用 `tools/obsidian_sync.py` 同步。

## 三條命令

```bash
# 1) 由資料庫匯出 Obsidian notes（每場一張）
python tools/obsidian_sync.py export --db racing-data/racing.db --vault "racing-data/vault"

# 2) 由 canonical notes 生成 card/result JSON
python tools/obsidian_sync.py import --vault "racing-data/vault" --out racing-data/obsidian

# 3) 舊 Hermes「Picks Summary」notes → 部分 card/result（migrate）
python tools/obsidian_sync.py migrate --vault "C:/Users/<you>/.../Obsidian Vault/賽馬" --out racing-data/legacy
```

`import`／`migrate` 只寫 JSON，唔會自動 `import-card`；確認內容後再逐個匯入：

```bash
python "<SKILL>/scripts/racing_lab.py" import-card --db racing-data/racing.db --file racing-data/obsidian/cards/<race_id>.json
```

## Canonical note 格式

```markdown
---
race_id: 2026-06-24-HV-R1
meeting_date: 2026-06-24
venue: HV
race_no: 1
off_time: 2026-06-24T19:15:00+08:00
distance_m: 2200
place_slots: 3
collection_mode: historical
---

## Runners
| No | Horse | Jockey | Trainer | Draw | Weight | Rating | Odds | Scratched |
|----|-------|--------|---------|------|--------|--------|------|-----------|

## Result
| No | Finish | Dividend |
|----|--------|----------|
```

- frontmatter 缺任何必要欄位 → `import` 會跳過該 note 並在 `skipped` 回報
- `Runners` 至少兩匹，否則跳過
- `Result` 可留空（未跑）

## 舊 notes 遷移：真實限制

舊 Hermes notes 只有「四匹貼士 + 命中標記 + Brier」，冇全場馬匹、冇賠率、冇完整名次。`migrate` 會：

- 產生 **partial card**（`partial: true`，`missing: ["odds","full_field","sectionals","intent_evidence"]`）
- 由 🥇🥈🥉 還原 **部分名次**（只限有獎牌的貼士），其餘場次冇 result
- 寫 `migration-report.json` 列明每張 note 抽到幾多場

**重點**：partial card 只可以做「記錄」同「未來補完」，**唔可以**當真實賽前預測用；`predict` 對呢類卡會用均勻市場基準並出 warning。

## 模板

ssets/obsidian-race-note.md 係可直接放進 vault Templates/ 的模板：frontmatter 八個欄位、Runners 表、Result 表。用 Obsidian 的「Insert template」即可開新賽事筆記。

實測過的兩種來源：

- **HKJC 官方賽果**：	ools/obsidian_sync.py 之外，用 hkjc_import 抓 archive/localresults，可補回全場、檔位、負磅、獨贏賠率、名次同派彩（2026-06-24 HV 9 場全部通過驗證）。
- **自由格式舊筆記**：Goodwood FINAL LOCK 那類散文式筆記，先轉成 canonical notes（見 Race Predictions/2026-07-31 Goodwood S4/canonical/），再用 import 生成 card JSON。

## 建議工作流

1. 賽前：用 skill 正常流程寫 card → `import-card` → `predict`
2. 賽後：`import-result`
3. 想用 Obsidian 睇／改：`export` 出 notes，改完再 `import` 回 JSON，重新 `import-card`（新快照）
4. 舊資料：`migrate` 一次，人工補齊賠率／名次後才匯入正式 DB

