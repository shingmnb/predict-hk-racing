# predict-hk-racing

[![test](https://github.com/shingmnb/predict-hk-racing/actions/workflows/test.yml/badge.svg)](https://github.com/shingmnb/predict-hk-racing/actions/workflows/test.yml)

香港賽馬情報分析 Skill：自動搜集及核實原件、分析能力／形勢／馬房去馬意圖，保存支持證據、反證及未知項，再用不可覆寫紀錄、賽後核查、時間順序回測及模型升級門檻持續修正。

> 呢個項目處理嘅「去馬」係基於公開證據嘅不確定推論，唔係內幕消息。冇已驗證模型時只出情報分析，唔會用賠率或均勻值硬砌勝率。賽馬高方差，請只用可承受損失作娛樂預算。

> 發佈狀態：GitHub Actions 會自動執行測試；版本 tag 會觸發 `.skill` 打包、SHA-256 同 GitHub Release。

## 功能

- **資料快照**：官方來源優先；保存來源、時間戳、退出及臨場更新。
- **情報分析**：逐匹保存能力、形勢、人馬部署、逐字引文、反證、未知及撤回條件。
- **原始賠率參考**：只保存可選市場快照與變化，唔會轉成概率或模型特徵。
- **可選概率模型**：conditional logit、Plackett–Luce 位置概率近似；冇模型時唔出概率。
- **審計紀錄**：SQLite 保存賽前快照、情報、預測、賽果、模型版本及評估；主要紀錄 append-only。
- **專業回測**：expanding-window walk-forward、均勻無資訊內部對照、log loss、Brier、校準及 race-level bootstrap。
- **有門檻學習**：候選模型要通過全部驗收及現任 champion 比較先可 promoted；否則記錄 rejected。
- **意圖 ablation**：驗證 `intent_centered` 有冇時間外預測價值，唔好用賽果反推動機。

## 安裝

- 而家可直接使用 repo 內嘅 [`predict-hk-racing/`](predict-hk-racing/) 源文件。
- 亦可從 [Releases](https://github.com/shingmnb/predict-hk-racing/releases) 下載 `predict-hk-racing.skill`，匯入支援 `.skill` 套件嘅 agent。
- 完整操作指引見 [`predict-hk-racing/SKILL.md`](predict-hk-racing/SKILL.md)。

## 快速開始

所有用戶資料預設放 `racing-data/`，已列入 `.gitignore`。腳本只用 Python 標準庫，建議 Python 3.11 或以上。

```bash
# 初始化本地紀錄庫
python predict-hk-racing/scripts/racing_lab.py init --db racing-data/racing.db

# 複製並填寫賽前資料模板
cp predict-hk-racing/assets/race-card-template.json racing-data/cards/race.json
python predict-hk-racing/scripts/racing_lab.py import-card \
  --db racing-data/racing.db --file racing-data/cards/race.json

# 凍結情報分析，不附勝出率
python predict-hk-racing/scripts/racing_lab.py analyze \
  --db racing-data/racing.db --race-id "<race_id>" \
  --output racing-data/analyses/<race_id>.json
```

賽後核查、可選模型訓練、回測及升級命令見 [`schemas-and-cli.md`](predict-hk-racing/references/schemas-and-cli.md)。

## Repo 結構

```text
predict-hk-racing/       # 可分發 Skill（SKILL.md、scripts、references、assets）
tests/                   # 標準庫 unittest；全部使用虛構資料
tools/package_skill.py   # Skill 驗證、打包及 SHA-256
.github/workflows/       # CI 測試及 tag Release
.github/ISSUE_TEMPLATE/  # Bug 與功能建議
CONTRIBUTING.md          # 維護及 PR 流程
CHANGELOG.md             # 版本紀錄
```

## 維護

```bash
# 執行全部測試
python -m unittest discover -s tests -v

# 只驗證 Skill 結構
python tools/package_skill.py --check-only

# 打包到 dist/
python tools/package_skill.py --output dist
```

採用 Semantic Versioning。推送 `v*.*.*` tag 會觸發測試、打包 `.skill`、產生 SHA-256 並建立 GitHub Release。詳見 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 數據與限制

- 唔好提交真實投注紀錄、個人資料、登入資料、付費內容或 `racing-data/`。
- 歷史重建唔可以冒充實時預測；任何特徵都要證明喺資料截止時間前已存在。
- 未夠樣本或冇 promoted 模型時只用 `analyze` 出情報，唔好自創權重或補概率。
- 模型表現會漂移；歷史績效唔保證未來，更唔保證投注回報。

## License

[MIT](LICENSE) © 2026 shingmnb
