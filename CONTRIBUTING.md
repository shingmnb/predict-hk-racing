# 維護與貢獻流程

## 基本原則

- 先開 Issue 說明問題、重現方法、預期結果同資料來源；較大改動先對齊設計。
- 由 `main` 建立 `feat/<主題>`、`fix/<主題>` 或 `docs/<主題>` branch，再開 Pull Request。
- 唔好直接提交真實投注紀錄、帳戶資料、個人資料、秘密、付費內容或本地 `racing-data/`。
- 測試只用虛構馬匹、虛構賽果同可公開測試資料。

## 修改 Skill

1. 保持 `predict-hk-racing/SKILL.md` 精簡；詳細方法放 `references/`，可重複執行嘅確定性邏輯放 `scripts/`。
2. Frontmatter 只保留 `name` 同 `description`；修改觸發場景時要同時檢查實際用途。
3. 新增或修改腳本後，必須更新測試及相關命令文件。
4. 新增資料欄位時，保持向後兼容或明確提升 schema 版本，並提供遷移方法。
5. 唔好將 README、安裝指南或 changelog 放入 Skill 套件內；呢啲文件只放 repo 根目錄。

## 模型與數據改動

- 每個特徵必須可按賽前快照重現，並證明喺 `data_cutoff` 前已存在。
- 唔好用隨機拆分取代時間順序驗證；訓練、填補、標準化、校準同調參都唔準見未來。
- 模型改動要附 walk-forward 結果、市場基準、現任 champion 比較、樣本數及 bootstrap 區間。
- 去馬意圖改動要做 `intent_centered` ablation；唔好用「贏咗所以去馬」作標籤。
- 單場爆冷、單場斷纜或少量 ROI 唔足以建立新規則。
- 未通過升級門檻嘅想法只列候選假設，唔可以改寫舊預測或舊評估。

## Pull Request 前檢查

```bash
python -m unittest discover -s tests -v
python tools/package_skill.py --check-only
python tools/package_skill.py --output dist
```

確認：

- 所有測試通過，Windows 與 Linux 路徑處理冇硬編碼依賴。
- 冇秘密、真實用戶數據、臨時檔、資料庫、`__pycache__` 或 `dist/` 進入 commit。
- README、SKILL 參考文件、模板同測試互相一致。
- PR 說明列出行為改變、兼容性、測試證據同風險。

## Release 流程

1. 將變更合入 `main`，確保 CI 通過。
2. 按 Semantic Versioning 更新 `CHANGELOG.md`：
   - MAJOR：不兼容 schema 或工作流程改變；
   - MINOR：向後兼容功能；
   - PATCH：向後兼容修正。
3. 建立並推送 tag，例如：

   ```bash
   git tag v0.1.1
   git push origin v0.1.1
   ```

4. Release workflow 會重跑測試、打包 `predict-hk-racing.skill`、產生 `SHA256SUMS.txt` 並建立 Release。
5. 下載 Release 附件再核對雜湊及安裝；如發現問題，另發修正版本，唔好覆寫已發佈資產。

## 定期維護

- 每次依賴更新 PR：檢查 Actions 版本、權限同測試結果。
- 每次模型更新：檢查數據漂移、特徵覆蓋率、校準及被拒絕版本原因。
- 每次文件更新：核對命令、路徑、模板同實際輸出。
- 任何自動化失效：先保留最後可用版本，如實記錄，唔好跳過測試或安全門檻。
