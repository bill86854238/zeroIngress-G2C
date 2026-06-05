# zeroIngress-G2C

本地端 Gmail 自動解析信件並同步到 Google Calendar，使用 Ollama 本地 LLM，資料完全在本機處理，不經過任何第三方雲端服務。

預設支援旅遊行程（機票、飯店、租車、機場接送），也可自行擴充關鍵字支援演唱會、醫療預約、餐廳訂位等任何有時間資訊的信件。

---

## 功能

- 自動篩選 Gmail 信件，依關鍵字比對
- 支援去程/回程航班自動拆分
- 全天事件自動跨日（飯店入住到退房）
- 重複事件去重（同航班號、同日期）
- 可疑解析結果自動標記（橘色警告，不寫入行事曆）
- 增量同步：已處理信件自動跳過，不重複寫入
- 選用 Brave Search API 補充飯店 check-in 時間與地址

---

## 需求

### 本地環境

- Python 3.11+
- [Ollama](https://ollama.com) 0.30.4+，已拉取支援 structured output 的模型（建議 `qwen3.6:35b-a3b` 或 `gemma4:e4b`）

### Google Cloud

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 建立或選擇一個專案
2. 啟用 **Gmail API** 與 **Google Calendar API**
3. 建立 OAuth 2.0 憑證（類型選「桌面應用程式」）
4. 下載憑證檔，檔名格式為 `client_secret_*.json`，放到專案目錄

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
MY_EMAIL=你的_Gmail_地址
BRAVE_API_KEY=你的_Brave_Search_API_Key
```

- `MY_EMAIL`：你的 Gmail 帳號，用來過濾掉自己寄出的信
- `BRAVE_API_KEY`：選填。可在 [Brave Search API](https://brave.com/search/api/) 申請，免費方案每月 2000 次查詢。未設定或不加 `--enrich` 時，Brave 功能完全不啟用

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

## 自訂關鍵字

編輯 `sync_now.py` 裡的 `KEYWORDS` 清單，可新增任何觸發詞：

```python
KEYWORDS = [
    "機票", "航班", "飯店", "hotel", "booking",  # 預設旅遊關鍵字
    "演唱會", "看診", "訂位",                      # 自行新增
]
```

---

## 篩選邏輯

**關鍵字篩選**：比對信件主旨與內文

**自動排除**：
- 取消通知信
- 登機提醒信（無完整行程資料）
- Re:/Fw: 開頭的往返信件
- 銀行帳單等非行程信件
- 自己寄出的信

---

## 注意事項

- `token.json` 與 `client_secret_*.json` 含有個人 Google 帳號憑證，**不要上傳到公開 repo**
- `processed.log` 記錄已處理的信件 ID，刪除後下次跑會重新處理全部信件
- LLM 解析結果可能有誤，建議先用 `--preview` 確認後再 `--live`
