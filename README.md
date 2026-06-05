# zeroIngress-G2C

本地端 Gmail → Google Calendar 自動同步工具。從 Gmail 篩選旅遊相關信件，用本地 Ollama LLM 解析出行程資訊，寫入 Google Calendar 的專用行事曆。

所有資料在本地處理，不經過任何第三方雲端服務。

---

## 功能

- 自動篩選機票、飯店、租車、機場接送等旅遊信件
- 支援去程/回程航班自動拆分
- 飯店事件自動設定為全天跨日事件
- 重複事件去重（同航班號、同日期飯店）
- 可疑幻覺自動標記（`--preview` 模式橘色警告）
- 增量同步：已處理信件自動跳過，不重複寫入
- 選用 Brave Search API 補充飯店 check-in 時間與地址

---

## 需求

### 本地環境

- Python 3.11+
- [Ollama](https://ollama.com) 0.30.4+，已拉取 `qwen3.6:35b-a3b`（或其他支援 structured output 的模型）

### Google Cloud

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 建立或選擇一個專案
2. 啟用 **Gmail API** 與 **Google Calendar API**
3. 建立 OAuth 2.0 憑證（類型選「桌面應用程式」）
4. 下載 `credentials.json`，檔名格式為 `client_secret_*.json`，放到專案目錄

---

## 安裝

```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 \
            google-api-python-client pydantic python-dotenv \
            beautifulsoup4 python-dateutil requests
```

---

## 設定

在專案目錄建立 `.env`：

```
BRAVE_API_KEY=你的_Brave_Search_API_Key
```

Brave API Key 可在 [Brave Search API](https://brave.com/search/api/) 申請，免費方案每月 2000 次查詢。若不需要飯店資訊補充，`.env` 可為空。

---

## 使用方式

```bash
# 模擬執行（不寫入行事曆）
python sync_now.py

# 輸出 HTML 預覽到 preview.html 並自動開啟瀏覽器
python sync_now.py --preview

# 增量寫入（自動跳過已處理的信件）
python sync_now.py --live

# 同上，並用 Brave Search 補充飯店資訊
python sync_now.py --live --enrich

# 清空處理記錄，重新處理全部信件
python sync_now.py --live --reset
```

**第一次執行**會開啟瀏覽器要求 Google OAuth 授權，完成後 `token.json` 會自動儲存，之後不需要重新授權。

---

## 行事曆

第一次寫入時會自動建立名為 **G2C AI Sync** 的專用行事曆。若要清空重來，直接在 Google Calendar 刪除整個行事曆即可，不影響其他行事曆。

---

## 篩選邏輯

**關鍵字篩選**：高鐵、台鐵、機票、航班、飯店、hotel、booking、reservation 等

**自動排除**：
- 取消通知信
- 登機提醒信（無完整行程資料）
- Re:/Fw: 開頭的 thread 回信
- 銀行帳單等非旅遊信件
- 自己寄出的信

---

## 注意事項

- `token.json` 與 `client_secret_*.json` 含有個人 Google 帳號憑證，**絕對不要上傳到公開 repo**
- `processed.log` 記錄已處理的信件 ID，刪除後下次跑會重新處理全部信件
- LLM 解析結果可能有誤，建議先用 `--preview` 確認後再 `--live`
