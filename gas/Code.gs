/**
 * Code.gs — G2C AI Sync (Google Apps Script + Groq Qwen3-32B)
 *
 * 安裝步驟：
 * 1. 前往 https://script.google.com 建立新專案
 * 2. 貼上此檔案全部內容
 * 3. 在 Script Properties 設定 GROQ_API_KEY
 * 4. 執行一次 setupTrigger() 設定每小時自動觸發
 * 5. 第一次執行時授權 Gmail + Calendar 存取權限
 */

// ── 設定區 ─────────────────────────────────────────────────────────────────────

const CONFIG = {
  CALENDAR_NAME: "G2C AI Sync",
  GROQ_MODEL: "llama-3.3-70b-versatile",
  BASE_YEAR: 2026,
  MAX_BODY_CHARS: 3000,
  SEARCH_DAYS: 90,
  PROCESSED_KEY: "processed_ids",
};

const KEYWORDS = [
  // 交通
  "高鐵", "台鐵", "train", "hsr",
  "機票", "航班", "flight", "airline", "boarding",
  "租車", "car rental",
  // 住宿
  "飯店", "hotel", "旅館", "hostel", "check-in", "check in",
  "訂房", "booking", "reservation",
  // 行程
  "itinerary", "行程",
  // 活動票券
  "演唱會", "concert", "展覽", "exhibition", "票券", "ticket",
  "入場券", "門票",
  // 醫療預約
  "看診", "門診", "掛號", "預約", "appointment",
  // 餐廳訂位
  "訂位", "餐廳預約",
];

const BLOCKED_SENDERS = [
  "cathaybk.com.tw",
  "skyinfo.jal.com",
  "skyinfo.ana.co.jp",
  "mh1.evaair.com",
];

const CHECKIN_NOTICE_KEYWORDS = [
  "online check in result",
  "check-in result",
  "ご搭乗のご案内",
  "boarding reminder",
  "check in reminder",
];

const DUPLICATE_SUBJECT_SENDER = [
  ["evaair.com", "electronic ticket"],
  ["evaair.com", "emd receipt"],
  ["amadeus.com", "emd receipt"],
];

const CANCEL_KEYWORDS = ["已取消", "cancelled", "canceled", "キャンセル", "取消", "cancellation"];

const SKIP_SUBJECT_PREFIXES = ["re:", "re：", "fw:", "fwd:"];

const AIRLINE_SIGNALS = [
  [["JAL", "Japan Airlines", "jal.com", "skyinfo.jal"], ["日本航空", "JAL", "JL"]],
  [["EVA AIR", "evaair", "長榮"], ["長榮", "EVA", "BR"]],
  [["星宇", "STARLUX", "starlux"], ["星宇", "STARLUX", "JX"]],
  [["ANA", "ana.co.jp"], ["全日空", "ANA", "NH"]],
  [["CATHAY", "cathay", "國泰航空"], ["國泰", "CATHAY", "CX"]],
];

// ── 觸發器設定 ─────────────────────────────────────────────────────────────────

/**
 * 設定每小時自動執行觸發器（只需執行一次）
 */
function setupTrigger() {
  // 刪除所有既有觸發器，避免重複
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));

  ScriptApp.newTrigger("syncEmails")
    .timeBased()
    .everyHours(1)
    .create();

  Logger.log("✅ 每小時觸發器已設定完成");
}

/**
 * 刪除所有觸發器
 */
function removeTriggers() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  Logger.log("觸發器已全部刪除");
}

// ── 主流程 ─────────────────────────────────────────────────────────────────────

/**
 * 主函式：掃描 Gmail → 解析 → 寫入行事曆
 * 可手動執行，也會被觸發器自動呼叫
 */
function syncEmails() {
  const startTime = new Date();
  Logger.log(`=== G2C AI Sync 開始 ${startTime.toISOString()} ===`);

  try {
    const apiKey = PropertiesService.getScriptProperties().getProperty("GROQ_API_KEY");
    if (!apiKey) {
      Logger.log("❌ 未設定 GROQ_API_KEY，請在 Script Properties 中新增");
      return;
    }

    const processedIds = _loadProcessed();
    const emails = _fetchCandidateEmails(processedIds);

    if (emails.length === 0) {
      Logger.log("沒有新郵件需要處理");
      return;
    }

    // 每次最多處理 20 封，避免超過 GAS 6 分鐘執行上限，剩下的下次觸發再跑
    const batch = emails.slice(0, 20);
    Logger.log(`找到 ${emails.length} 封新郵件，本次處理 ${batch.length} 封`);

    const calendarId = _getOrCreateCalendar();
    const eventsData = [];

    for (let i = 0; i < batch.length; i++) {
      const email = batch[i];
      Logger.log(`[${i + 1}/${batch.length}] 處理: ${email.subject.substring(0, 60)}`);

      // Groq 免費版限速，每封間隔 3 秒
      if (i > 0) Utilities.sleep(3000);

      try {
        const parsedEvents = _parseEmailWithGemini(email, apiKey);

        if (!parsedEvents || parsedEvents.length === 0) {
          Logger.log("  → 無旅遊事件，略過");
          _saveProcessed(email.id);
          continue;
        }

        for (const event of parsedEvents) {
          const warning = _checkSuspicious(email, event);
          eventsData.push({
            status: warning ? "suspicious" : "ok",
            email: email,
            event: event,
            reason: warning || "",
          });
        }
        _saveProcessed(email.id);

      } catch (err) {
        Logger.log(`  ⚠️ 解析失敗: ${err.message}`);
        // 429/503 不標記已處理，下次觸發自動重試
        if (!err.message.includes("429") && !err.message.includes("503")) {
          _saveProcessed(email.id);
        }
        eventsData.push({
          status: "fail",
          email: email,
          event: null,
          reason: err.message,
        });
      }
    }

    // 去重
    const deduped = _dedupEvents(eventsData);

    // 寫入行事曆
    let successCount = 0;
    let skipCount = 0;
    let failCount = 0;

    for (const item of deduped) {
      if (item.status === "suspicious") {
        Logger.log(`  ⚠️ 略過可疑事件: ${item.event.title} — ${item.reason}`);
        _saveProcessed(item.email.id);
        skipCount++;
        continue;
      }
      if (item.status !== "ok") {
        failCount++;
        continue;
      }

      try {
        _insertEvent(calendarId, item.event);
        Logger.log(`  ✅ 已建立: ${item.event.title}`);
        _saveProcessed(item.email.id);
        successCount++;
      } catch (err) {
        Logger.log(`  ❌ 寫入失敗: ${err.message}`);
        failCount++;
      }
    }

    const elapsed = ((new Date() - startTime) / 1000).toFixed(1);
    Logger.log(`=== 完成 | 成功: ${successCount} | 略過: ${skipCount} | 失敗: ${failCount} | 耗時: ${elapsed}s ===`);

  } catch (err) {
    Logger.log(`❌ 執行錯誤: ${err.message}\n${err.stack}`);
  }
}

// ── Gmail 篩選 ─────────────────────────────────────────────────────────────────

function _fetchCandidateEmails(processedIds) {
  const keywordQuery = KEYWORDS.map(kw => `"${kw}"`).join(" OR ");
  const query = `(${keywordQuery}) newer_than:${CONFIG.SEARCH_DAYS}d`;

  const threads = GmailApp.search(query, 0, 500);
  const emails = [];
  const myEmail = Session.getEffectiveUser().getEmail().toLowerCase();

  for (const thread of threads) {
    const messages = thread.getMessages();
    for (const msg of messages) {
      const id = msg.getId();

      // 已處理過的略過
      if (processedIds.has(id)) continue;

      const sender = msg.getFrom().toLowerCase();
      const subject = msg.getSubject().toLowerCase();

      // 略過自己寄出的
      if (sender.includes(myEmail)) continue;

      // 略過封鎖寄件者
      if (BLOCKED_SENDERS.some(domain => sender.includes(domain))) continue;

      // 略過取消通知
      const bodyPreview = (msg.getPlainBody() || "").substring(0, 500).toLowerCase();
      if (CANCEL_KEYWORDS.some(kw => subject.includes(kw.toLowerCase()) || bodyPreview.includes(kw.toLowerCase()))) continue;

      // 略過 check-in 提醒
      if (CHECKIN_NOTICE_KEYWORDS.some(kw => subject.includes(kw.toLowerCase()))) continue;

      // 略過 Re:/Fw: 回覆信
      if (SKIP_SUBJECT_PREFIXES.some(prefix => subject.startsWith(prefix))) continue;

      // 略過已知重複的 sender+subject 組合
      if (DUPLICATE_SUBJECT_SENDER.some(([domain, kw]) => sender.includes(domain) && subject.includes(kw))) continue;

      const body = _extractBody(msg);

      emails.push({
        id: id,
        subject: msg.getSubject(),
        sender: msg.getFrom(),
        date: msg.getDate().toISOString(),
        body: body,
      });
    }
  }

  return emails;
}

function _extractBody(msg) {
  let body = msg.getPlainBody() || "";

  // 如果純文字為空，改用 HTML 轉純文字
  if (!body.trim()) {
    const htmlBody = msg.getBody() || "";
    body = htmlBody
      .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "")
      .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/g, " ")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"');
  }

  // 整理空白
  body = body.replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();

  // 在付款/頁尾區截斷
  const cutoffPatterns = [
    /付款明細/, /價格摘要/, /Payment Summary/, /Price Summary/,
    /unsubscribe/i, /copyright.*all rights reserved/i,
    /本メールの記事を許可無く/,
  ];
  for (const pattern of cutoffPatterns) {
    const match = body.match(pattern);
    if (match && match.index > 300) {
      body = body.substring(0, match.index).trim();
      break;
    }
  }

  return body.substring(0, CONFIG.MAX_BODY_CHARS);
}

// ── Groq 解析 ─────────────────────────────────────────────────────────────────

function _parseEmailWithGemini(email, apiKey) {
  const systemPrompt = `You are a travel itinerary extraction assistant.
Today's base year is ${CONFIG.BASE_YEAR}. All relative dates (e.g. "this Friday", "next Monday", "06/22")
must be interpreted relative to ${CONFIG.BASE_YEAR}.

Extract calendar events from the email. Output ONLY valid JSON with these exact keys:

- title: descriptive event name. Follow these rules:
  * For flights: include airline name, flight number, and route. Format: "長榮航空 BR801 — 桃園T2 → 札幌新千歲"
  * For car rentals: include vendor and pickup location. Format: "Times 租車 — 旭川站前"
  * For hotels: include hotel name. Format: "Dormy Inn PREMIUM Kushiro 入住"
  * For transfers/shuttles: include service and route. Format: "Klook 送機 — 樹林→桃園T1"
  * Always use Traditional Chinese where possible.

- start: ISO 8601 datetime with timezone +08:00 (e.g. "${CONFIG.BASE_YEAR}-06-22T10:30:00+08:00").
  * For flights: use departure time.
  * If only a date is known with no time, use T00:00:00+08:00.

- end: ISO 8601 datetime with timezone +08:00.
  * For flights: use arrival time at destination. If not stated, estimate based on typical route duration.
  * For hotels: use the CHECKOUT date (not check-in date) at T00:00:00+08:00. The end date MUST be different from start date.
  * For car rentals: use the return date at the return time.
  * If only a date is known, use T00:00:00+08:00.

- location: specific and useful location string.
  * For flights: "出發航廈 → 目的地機場" — MUST match the actual direction of THIS flight leg
  * For hotels: city name or hotel address
  * For car rentals: pickup location name/address
  * For transfers: pickup address → drop-off address

- description: concise summary in Traditional Chinese only (no other languages). 2~4 lines max.

IMPORTANT rules:
- If the email is an EMD receipt, ancillary fee receipt, meal pre-order, Wi-Fi purchase, or check-in reminder without full itinerary details, output empty events array.
- If the email is a cancellation notice, output empty events array.
- For flights: extract airline name and flight number ONLY from the flight itinerary table. Never use airline/flight info from signatures or unrelated text.
- For hotels: only extract check-in dates that are explicitly stated. Do NOT invent additional hotel stays.
- Each event must be a distinct, real travel segment. Do not duplicate or fabricate events.

Output format: always return a JSON object with an "events" array.
- Single event: {"events": [{...}]}
- Round-trip flights: {"events": [{outbound leg...}, {return leg...}]}
- No travel event: {"events": []}`;

  const userContent = `Subject: ${email.subject}\nFrom: ${email.sender}\nDate: ${email.date}\n\n${email.body}`;

  const payload = {
    model: CONFIG.GROQ_MODEL,
    temperature: 0,
    messages: [
      { role: "system", content: systemPrompt },
      { role: "user",   content: userContent },
    ],
    response_format: { type: "json_object" },
  };

  const url = "https://api.groq.com/openai/v1/chat/completions";
  const options = {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: `Bearer ${apiKey}` },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  };

  let resp;
  for (let attempt = 0; attempt < 3; attempt++) {
    resp = UrlFetchApp.fetch(url, options);
    const code = resp.getResponseCode();
    if (code === 200) break;
    if ((code === 429 || code === 503) && attempt < 2) {
      Logger.log(`  Groq ${code}，等待 ${(attempt + 1) * 10}s 後重試...`);
      Utilities.sleep((attempt + 1) * 10000);
      continue;
    }
    throw new Error(`Groq API 回傳 ${code}: ${resp.getContentText().substring(0, 200)}`);
  }
  const code = resp.getResponseCode();
  if (code !== 200) {
    throw new Error(`Groq API 回傳 ${code}: ${resp.getContentText().substring(0, 200)}`);
  }

  const json = JSON.parse(resp.getContentText());
  const rawText = json?.choices?.[0]?.message?.content;
  if (!rawText) {
    throw new Error("Groq 回傳空內容");
  }

  let wrapper;
  try {
    wrapper = JSON.parse(rawText);
  } catch (e) {
    throw new Error(`JSON 解析失敗: ${rawText.substring(0, 100)}`);
  }

  const items = wrapper.events || [];
  const results = [];

  for (const data of items) {
    if (!data.title || !data.start) continue;

    try {
      data.start = _normaliseDt(data.start);
      data.end = data.end ? _normaliseDt(data.end) : _addOneHour(data.start);
      data.isAllDay = _isMidnight(data.start) && _isMidnight(data.end);

      // 全天事件：若 start == end 則 end + 1 day
      if (data.isAllDay && data.start.substring(0, 10) === data.end.substring(0, 10)) {
        const d = new Date(data.end);
        d.setDate(d.getDate() + 1);
        data.end = _toIso(d);
      }

      // 非全天：確保 end > start
      if (!data.isAllDay) {
        const s = new Date(data.start);
        const e = new Date(data.end);
        if (e <= s) {
          s.setHours(s.getHours() + 1);
          data.end = _toIso(s);
        }
      }

      results.push(data);
    } catch (err) {
      Logger.log(`  日期正規化失敗: ${err.message}`);
    }
  }

  return results;
}

// ── 行事曆操作 ─────────────────────────────────────────────────────────────────

function _getOrCreateCalendar() {
  const calendars = CalendarApp.getAllCalendars();
  for (const cal of calendars) {
    if (cal.getName() === CONFIG.CALENDAR_NAME) {
      return cal.getId();
    }
  }
  // 建立新行事曆
  const newCal = CalendarApp.createCalendar(CONFIG.CALENDAR_NAME);
  Logger.log(`[Calendar] 已建立新行事曆: ${CONFIG.CALENDAR_NAME}`);
  return newCal.getId();
}

function _insertEvent(calendarId, event) {
  const cal = CalendarApp.getCalendarById(calendarId);
  if (!cal) throw new Error(`找不到行事曆 ${calendarId}`);

  const startDate = new Date(event.start);
  const endDate = new Date(event.end);

  const options = {
    description: event.description || "",
    location: event.location || "",
  };

  if (event.isAllDay) {
    // 全天事件
    cal.createAllDayEventSeries(
      event.title,
      startDate,
      CalendarApp.newRecurrence().addDailyRule().times(1),
      options
    );
    // 上面的方法會建立重複事件，改用直接方式
    // GAS 沒有直接 createAllDayEventRange，用 createAllDayEvent + 修改
    // 實際做法：使用 Calendar REST API
    _insertEventViaApi(calendarId, event);
    return;
  }

  cal.createEvent(event.title, startDate, endDate, options);
}

/**
 * 使用 Calendar REST API 插入事件（支援全天事件與多天範圍）
 */
function _insertEventViaApi(calendarId, event) {
  let body;

  if (event.isAllDay) {
    body = {
      summary: event.title,
      location: event.location || "",
      description: event.description || "",
      start: { date: event.start.substring(0, 10) },
      end:   { date: event.end.substring(0, 10) },
    };
  } else {
    body = {
      summary: event.title,
      location: event.location || "",
      description: event.description || "",
      start: { dateTime: event.start, timeZone: "Asia/Taipei" },
      end:   { dateTime: event.end,   timeZone: "Asia/Taipei" },
    };
  }

  const token = ScriptApp.getOAuthToken();
  const url = `https://www.googleapis.com/calendar/v3/calendars/${encodeURIComponent(calendarId)}/events`;
  const options = {
    method: "post",
    contentType: "application/json",
    headers: { Authorization: `Bearer ${token}` },
    payload: JSON.stringify(body),
    muteHttpExceptions: true,
  };

  const resp = UrlFetchApp.fetch(url, options);
  if (resp.getResponseCode() !== 200) {
    throw new Error(`Calendar API 回傳 ${resp.getResponseCode()}: ${resp.getContentText().substring(0, 200)}`);
  }
}

// ── 去重邏輯 ───────────────────────────────────────────────────────────────────

function _flightNumber(title) {
  const m = title.match(/\b([A-Z]{2})\s*(\d{2,4})\b/);
  return m ? `${m[1]}${m[2]}` : null;
}

function _dedupEvents(eventsData) {
  const seen = new Set();
  return eventsData.map(item => {
    if (!item.event) return item;
    const event = item.event;
    const fn = _flightNumber(event.title);
    let key;
    if (fn) {
      key = `${fn}|${event.start.substring(0, 10)}`;
    } else {
      key = `${event.start.substring(0, 10)}|${event.end.substring(0, 10)}|${event.title.substring(0, 8).trim()}`;
    }
    if (seen.has(key)) {
      return { ...item, status: "skip", reason: "重複事件" };
    }
    seen.add(key);
    return item;
  });
}

// ── 航班幻覺偵測 ───────────────────────────────────────────────────────────────

function _checkSuspicious(email, event) {
  const haystack = `${email.subject} ${email.sender}`.toLowerCase();
  for (const [signals, expectedKeywords] of AIRLINE_SIGNALS) {
    if (signals.some(s => haystack.includes(s.toLowerCase()))) {
      if (!expectedKeywords.some(k => event.title.toLowerCase().includes(k.toLowerCase()))) {
        const matched = signals.find(s => haystack.includes(s.toLowerCase()));
        return `寄件者含 '${matched}' 但標題為 '${event.title}'，可能是幻覺`;
      }
    }
  }
  return null;
}

// ── processed 記錄 ─────────────────────────────────────────────────────────────

function _loadProcessed() {
  const props = PropertiesService.getScriptProperties();
  const raw = props.getProperty(CONFIG.PROCESSED_KEY) || "[]";
  try {
    return new Set(JSON.parse(raw));
  } catch (e) {
    return new Set();
  }
}

function _saveProcessed(msgId) {
  const props = PropertiesService.getScriptProperties();
  const raw = props.getProperty(CONFIG.PROCESSED_KEY) || "[]";
  let ids;
  try {
    ids = JSON.parse(raw);
  } catch (e) {
    ids = [];
  }

  if (!ids.includes(msgId)) {
    ids.push(msgId);
    // PropertiesService 有 9KB 上限，保留最近 2000 筆
    if (ids.length > 2000) {
      ids = ids.slice(ids.length - 2000);
    }
    props.setProperty(CONFIG.PROCESSED_KEY, JSON.stringify(ids));
  }
}

/**
 * 清除所有已處理記錄（重新處理全部信件用）
 */
function resetProcessed() {
  PropertiesService.getScriptProperties().deleteProperty(CONFIG.PROCESSED_KEY);
  Logger.log("已清除 processed 記錄");
}

// ── 日期工具 ───────────────────────────────────────────────────────────────────

function _normaliseDt(dtStr) {
  // 補上時區：若無 +/Z 則視為 +08:00
  if (!/[Z+\-]\d{2}:?\d{2}$/.test(dtStr) && !/Z$/.test(dtStr)) {
    dtStr += "+08:00";
  }
  const d = new Date(dtStr);
  if (isNaN(d.getTime())) throw new Error(`無法解析日期: ${dtStr}`);
  return _toIso(d);
}

function _addOneHour(dtStr) {
  const d = new Date(dtStr);
  d.setHours(d.getHours() + 1);
  return _toIso(d);
}

function _isMidnight(dtStr) {
  const d = new Date(dtStr);
  // 在 +08:00 時區中是否為 00:00:00
  const utcOffset = 8 * 60; // minutes
  const localMinutes = (d.getUTCHours() * 60 + d.getUTCMinutes() + utcOffset) % (24 * 60);
  return localMinutes === 0 && d.getUTCSeconds() === 0;
}

function _toIso(d) {
  // 回傳帶 +08:00 的 ISO 字串
  const pad = n => String(n).padStart(2, "0");
  const offset = 8 * 60;
  const local = new Date(d.getTime() + offset * 60000);
  return `${local.getUTCFullYear()}-${pad(local.getUTCMonth() + 1)}-${pad(local.getUTCDate())}T${pad(local.getUTCHours())}:${pad(local.getUTCMinutes())}:${pad(local.getUTCSeconds())}+08:00`;
}

// ── 手動工具函式 ────────────────────────────────────────────────────────────────

/**
 * 由 clasp run 呼叫，從外部傳入 API key 寫入 Script Properties
 * 用法：clasp run setProperties --params '[["YOUR_GEMINI_KEY"]]'
 */
function setProperties(geminiApiKey) {
  PropertiesService.getScriptProperties().setProperty("GEMINI_API_KEY", geminiApiKey);
  Logger.log("✅ GEMINI_API_KEY 已設定");
}

/**
 * 列出目前已設定的觸發器（Debug 用）
 */
function listTriggers() {
  const triggers = ScriptApp.getProjectTriggers();
  triggers.forEach(t => {
    Logger.log(`觸發器: ${t.getHandlerFunction()} — ${t.getTriggerSource()}`);
  });
  if (triggers.length === 0) Logger.log("目前沒有觸發器");
}

/**
 * 顯示已處理郵件 ID 數量（Debug 用）
 */
function showProcessedCount() {
  const ids = _loadProcessed();
  Logger.log(`已處理郵件數: ${ids.size}`);
}
