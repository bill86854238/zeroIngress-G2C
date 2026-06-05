# zeroIngress-G2C

[English](README.en.md)

自動從 Gmail 篩選旅遊、交通、活動相關信件，透過 AI 解析出行程資訊，寫入 Google Calendar。

目前有兩個版本可選：

| | v2 Google Apps Script | v1 本地 Python |
|---|---|---|
| 執行方式 | 每小時自動在雲端執行 | 手動在本機執行 |
| LLM | 可換（見下方） | Ollama（本地模型） |
| 需要本機常駐 | 否 | 否（手動觸發） |
| 隱私 | 信件資料傳送給雲端 LLM | 資料留在本機 |
| 設定難度 | 簡單（只需貼上 script） | 需要安裝 Python、Ollama |

> **v1** 已 tag 為 `v1.0-local-ollama`，有隱私需求可切換到該版本。

---

## v2 LLM 選擇與注意事項

v2 目前支援兩種 LLM，在 `gas/Code.gs` 的 `CONFIG` 區塊切換：

### 選項 A：Gemini（Google）

```js
GROQ_MODEL: undefined,  // 不填
// 改用 GEMINI_MODEL
```

- **免費版**：`gemini-2.5-flash`，每天 20 次請求，45 封信一次就超標
- **付費版**：買 NT$1000 credits（非訂閱，一次買斷），1000 RPM，夠用好幾年，約 NT$0.3/次
- Script Properties key 名稱：`GEMINI_API_KEY`

### 選項 B：Groq（目前使用）

```js
GROQ_MODEL: "llama-3.3-70b-versatile",
```

- **免費版限制**：每天 100,000 token（約 50～80 封信），超過當天就無法使用
- 一次性有大量舊信件要補跑時容易撞到每日上限
- 日常每小時新信件 1～2 封則完全夠用
- Script Properties key 名稱：`GROQ_API_KEY`，在 [console.groq.com/keys](https://console.groq.com/keys) 取得

> **實務建議**：日常使用 Groq 免費版即可；若有大量舊信件要一次補跑，可暫時買 Gemini credits 跑完後再換回來。

---

## 功能

- 自動篩選 Gmail 信件，依旅遊、住宿、活動關鍵字比對
- 支援去程/回程航班自動拆分
- 全天事件自動跨日（飯店入住到退房）
- 重複事件去重（同航班號、同日期）
- 可疑解析結果（如航班資訊幻覺）自動略過
- 增量同步：已處理信件自動跳過

---

## v2：Google Apps Script 版（推薦）

不需要本機環境，設定一次後每小時自動執行。

### 需求

- Google 帳號（Gmail + Google Calendar 即可）
- Gemini API Key（免費方案即足夠）：前往 [Google AI Studio](https://aistudio.google.com/apikey) 建立

### 安裝步驟

1. 前往 [Google Apps Script](https://script.google.com)，點「New project」
2. 將 `gas/Code.gs` 的全部內容貼入編輯器（覆蓋預設的 `myFunction`）
3. 點選上方選單 **Project Settings** → **Script Properties** → 新增：
   - Key: `GEMINI_API_KEY`　Value: 你的 Gemini API Key
4. 在函式下拉選單選 `setupTrigger`，點執行（⏵），授權 Gmail + Calendar 存取權限
5. 完成。之後每小時會自動執行 `syncEmails`

### 手動觸發

在 GAS 編輯器中，選 `syncEmails` 後點執行即可立即跑一次。

### 查看執行記錄

GAS 編輯器左側 **Executions** 可看到每次執行的 Logger 輸出。

### 重設處理記錄

執行 `resetProcessed()` 可清除已處理記錄，下次執行會重新掃描全部信件。

---

## v1：本地 Python 版

資料完全在本機處理，不經過任何第三方雲端 LLM 服務。

> 切換到 `v1.0-local-ollama` tag 取得穩定版本：`git checkout v1.0-local-ollama`

### 需求

- Python 3.11+
- [Ollama](https://ollama.com) 0.30.4+，已拉取支援 structured output 的模型（建議 `qwen3.6:35b-a3b`）
- Google Cloud OAuth 憑證（Gmail API + Google Calendar API）

### 安裝

```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 \
            google-api-python-client pydantic python-dotenv \
            beautifulsoup4 python-dateutil requests
```

### 設定

```bash
cp .env.example .env
```

在 `.env` 填入：

```
MY_EMAIL=你的_Gmail_地址
```

### 使用方式

```bash
# HTML 預覽（不寫入）
python sync_now.py --preview

# 寫入行事曆
python sync_now.py --live

# 清空記錄，重新處理全部信件
python sync_now.py --reset
```

---

## 行事曆

第一次寫入時會自動建立名為 **G2C AI Sync** 的專用行事曆。若要清空重來，直接在 Google Calendar 刪除整個行事曆即可，不影響其他行事曆。

---

## 自訂關鍵字

**v2**：編輯 `gas/Code.gs` 裡的 `KEYWORDS` 陣列。

**v1**：編輯 `sync_now.py` 裡的 `KEYWORDS` 清單。

預設支援旅遊行程（機票、飯店、租車、機場接送）、活動票券（演唱會、展覽）、醫療預約、餐廳訂位等。

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

- **v2**：Gemini API Key 存放在 GAS Script Properties，不會暴露在程式碼中
- **v1**：`token.json` 與 `client_secret_*.json` 含有個人 Google 憑證，不要上傳到公開 repo
