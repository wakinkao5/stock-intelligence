# 台股智慧決策中心 V2

此版本沿用 V1 首頁，將 **加權指數收盤、成交金額、最近最多 30 交易日收盤線圖** 改為 TWSE 官方資料，沒有資料時顯示未取得，不會顯示虛構數值。

## 部署

1. 將本壓縮檔內的 `index.html`、`scripts/update_twse.py`、`.github/workflows/update-taiex.yml`、`README.md` 上傳至 GitHub 儲存庫的對應位置（不要上傳外層資料夾）。
2. GitHub 儲存庫 Settings → Actions → General → Workflow permissions 選 **Read and write permissions**（若已允許則不必更改）。
3. Actions → **更新台股官方收盤資料** → **Run workflow**，先手動執行一次，確認綠色成功狀態。
4. 此後每個週一至週五台灣時間 **16:45** 嘗試更新；GitHub 定時排程可能延遲，國定假日若無新資料就維持最近交易日並顯示日期。
5. GitHub Pages 部署成功後，開啟首頁檢查來源與交易日期。

## 範圍與限制

- TWSE API: `https://www.twse.com.tw/indicesReport/FMTQIK?response=json&date=YYYYMM01`
- **不是即時行情**。折線圖是逐日收盤，不是 09:00～13:30 的盤中線。
- 上櫃指數、海外市場、AI 預測、四時段速報同步、個股健檢與個人資產 **尚未串接**。
- 只有成功取得 TWSE 資料才更新檔案；請檢查 Actions 日誌。若官方 API 改版，需修改解析程式。
- 公開 GitHub 儲存庫不得放入個人持股、密碼或 API 金鑰。
