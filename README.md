# 🏗️ Agentic Prop Firm Execution Engine (APFEE)
### Complete Step-by-Step Build Guide

> **Core Principle:** The system makes decisions. The human approves. Execution follows rules only.

---

## ✅ Build Status

| Step | Description | Status |
|------|-------------|--------|
| 1 | Create Telegram Bot | ✅ Complete |
| 2 | Make.com Scenario Skeleton | ✅ Complete |
| 3 | Signal Intake + Filter | ✅ Complete |
| 4 | Claude API Connection | ✅ Complete |
| 5 | Parse JSON + Router | ✅ Complete |
| 6 | Fast Gate Filters | ✅ Complete |
| 7 | Google Sheets State Store | ✅ Complete |
| 8 | Enforcement Filters | ✅ Complete |
| 9 | Report Formatting | ✅ Complete |
| 10 | Approval Loop (YES/NO) | ✅ Complete |
| 11 | Test Scenarios (all 5) | ✅ Complete |
| 12 | Paper Trading Day | ⏳ Next |
| 13 | Go Live (Manual MT5) | ⏳ Pending |

---

## What You Are Building

A fully automated trade filter and decision engine that:
1. Receives trading signals from Telegram
2. Runs them through strict prop firm compliance rules
3. Uses Claude AI to grade the setup
4. Sends you a formatted report asking YES or NO
5. Updates its own state after every trade

You do **not** need to code anything. This is built entirely inside Make.com, Telegram, and Claude's API.

---

## What You Need Before Starting

- A Telegram account (mobile app installed)
- A Make.com account (free plan works to start)
- An Anthropic account for Claude API access
- A MetaTrader 5 account (for execution later)
- A Google account (for Google Sheets as state store)

---

## System Architecture

```
[Telegram Signal]
      │
      ▼
[Webhook Trigger] ← Custom Webhook (Make.com)
      │
      ▼
[Router 1] ── YES/NO ──→ [Approval Handler Scenario]
      │                        │
      │ BUY/SELL               ▼
      ▼               [Google Sheets Update]
[Google Sheets]              │
(Read State)                 ▼
      │               [Telegram Confirmation]
      ▼
[Router 2 - Fast Gates]
      │ fail
      ▼
[🚫 BLOCKED Message]
      │ pass
      ▼
[HTTP - Claude API]
      │
      ▼
[JSON Parser]
      │
      ▼
[Router 3 - Decision]
      ├── EXECUTE → [Enforcement Filter] → [Report] → [Partner Report]
      └── BLOCK → [⛔ Block Message]
```

---

## Two Scenarios

### APFEE — Main Engine
Handles incoming signals, runs gates, calls Claude, sends reports.

### APFEE — Approval Handler
Handles YES/NO replies, updates Google Sheets, sends confirmation.

---

## Bots Used

| Bot | Purpose |
|-----|---------|
| APFEE Signal Engine (@APFEESignalBot) | Receives signals, sends reports |
| Second bot (separate token) | Registered to Main Engine webhook |

> **Important:** Always use a dedicated bot token for APFEE. Never share a bot token across multiple Make.com scenarios.

---

# STEP 1 — Create Your Telegram Bot

**1.1** Open Telegram and search `@BotFather`

**1.2** Tap **BotFather** (blue verified checkmark) → tap **START**

**1.3** Type and send: `/newbot`

**1.4** Enter display name: `APFEE Signal Engine`

**1.5** Enter username: `APFEESignalBot` (must end in `bot`)

**1.6** Copy the **API Token** shown — save it in Notes. Format: `1234567890:ABCDef...`

**1.7** Go to your bot in Telegram and press **START**

---

# STEP 2 — Create Make.com Scenario Skeleton

**2.1** Go to [make.com](https://make.com) → **Scenarios** → create folder `APFEE — Prop Firm Engine`

**2.2** Click **+ Create a new scenario**

**2.3** Click the `+` circle → search `Webhooks` → select **Custom Webhook**

**2.4** Click **Add** → name it `APFEE Main Webhook` → click **Save**

**2.5** Copy the webhook URL shown

**2.6** Register it with Telegram — paste this in your browser:
```
https://api.telegram.org/bot[YOUR_TOKEN]/setWebhook?url=[YOUR_WEBHOOK_URL]
```

**2.7** You should see: `{"ok":true,"result":true,"description":"Webhook was set"}`

**2.8** Toggle **Immediately as data arrives** ON at the bottom

**2.9** Save the scenario as `APFEE — Main Engine`

---

# STEP 3 — Add Signal Router

**3.1** After the Webhook module, add a **Router** (Flow Control → Router)

**3.2** Set up two paths:

**Path 1 — Signal:**
- `message.text` Contains `BUY`
- OR `message.text` Contains `buy`
- OR `message.text` Contains `SELL`
- OR `message.text` Contains `sell`

**Path 2 — Approval:**
- `message.text` Equal to `YES`
- OR `message.text` Equal to `yes`
- OR `message.text` Equal to `NO`
- OR `message.text` Equal to `no`

**3.3** On Path 2, add **HTTP → Make a Request**:
- URL: your Approval Handler webhook URL
- Method: `POST`
- Body Type: `Raw`
- Content Type: `JSON`
- Body:
```json
{
  "reply": "{{1.message.text}}",
  "chat_id": "{{1.message.chat.id}}"
}
```

---

# STEP 4 — Connect Claude API

**4.1** Get your Claude API key at [console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key

**4.2** On the Signal path (Path 1), add **Google Sheets → Search Rows** first (see Step 7)

**4.3** After Google Sheets, add **HTTP → Make a Request**:

- **URL:** `https://api.anthropic.com/v1/messages`
- **Method:** `POST`
- **Headers:**

| Key | Value |
|-----|-------|
| `x-api-key` | your `sk-ant-...` key |
| `anthropic-version` | `2023-06-01` |
| `Content-Type` | `application/json` |

- **Body Type:** `Raw`
- **Content Type:** `JSON`
- **Body:**
```json
{
  "model": "claude-sonnet-4-20250514",
  "max_tokens": 1000,
  "system": "You must respond with ONLY a raw JSON object. No markdown, no code fences, no backticks, no explanation. Just the JSON object starting with { and ending with }. You are a prop firm trade filter. Analyze the trading signal and return ONLY a valid JSON object with these exact fields: pair, direction, grade (A+/A/BLOCK), decision (EXECUTE/BLOCK), risk_percent (0.5 for A+, 0.35 for A, 0 for BLOCK), trend, confirmation (true/false), structure (true/false), zone (true/false), liquidity (true/false), retracement_valid (true/false), correlation_conflict (true/false), session_valid (true/false), reason. Grade A+ only for perfect setups with trend alignment, clear structure, valid zone, liquidity sweep, and confirmation. Grade A for good setups missing one minor element. BLOCK everything else.",
  "messages": [
    {
      "role": "user",
      "content": "{{1.message.text}}"
    }
  ]
}
```

---

# STEP 5 — Parse JSON + Decision Router

**5.1** After HTTP module, add **JSON → Parse JSON**

**5.2** JSON String field: select `Data → Body` from HTTP output

**5.3** Generate data structure using this sample:
```json
{
  "pair": "GBPUSD",
  "direction": "BUY",
  "grade": "A+",
  "decision": "EXECUTE",
  "risk_percent": 0.5,
  "trend": "bullish",
  "confirmation": true,
  "structure": true,
  "zone": true,
  "liquidity": true,
  "retracement_valid": true,
  "correlation_conflict": false,
  "session_valid": true,
  "reason": "Clean setup"
}
```

**5.4** Add a **Router** after JSON:
- **Path 1 (EXECUTE):** `decision` Equal to `EXECUTE`
- **Path 2 (BLOCK):** `decision` Equal to `BLOCK`

**5.5** On BLOCK path add Telegram message:
```
⛔ {{pair}}{{direction}} - BLOCKED
Reason: {{reason}}
```

---

# STEP 6 — Fast Gate Filters

**6.1** After Google Sheets (read state), add a **Router** with two paths:

**Path 1 — Gates Pass:**
- `{{parseNumber(formatDate(now; "HH"))}}` Greater than or equal to `7`
- AND `{{parseNumber(formatDate(now; "HH"))}}` Less than or equal to `21`
- AND `trades_today (A)` Less than `3`
- AND `open_trades (B)` Less than `2`
- AND `live_exposure (C)` Less than `1`

**Path 2 — Gates Fail (Fallback):**
- Set as fallback route
- Add Telegram message:
```
🚫 SIGNAL BLOCKED
Daily limit, exposure, or session violation.
Check your Google Sheet and try again.
```

---

# STEP 7 — Google Sheets State Store

**7.1** Go to [sheets.google.com](https://sheets.google.com) → create new spreadsheet → name it `APFEE State Store`

**7.2** Rename Sheet1 to `State`

**7.3** Add headers in Row 1:

| A | B | C | D | E | F | G | H | I |
|---|---|---|---|---|---|---|---|---|
| trades_today | open_trades | live_exposure | last_trade_time | session_losses | win_streak | daily_pnl | challenge_mode | endgame_mode |

**7.4** Add starting values in Row 2:
```
0 | 0 | 0 | (blank) | 0 | 0 | 0 | TRUE | FALSE
```

**7.5** In Make.com, on the Signal path after Router 1, add **Google Sheets → Search Rows**:
- Spreadsheet: `APFEE State Store`
- Sheet: `State`
- Filter: Column A — exists
- Limit: `1`

---

# STEP 8 — Enforcement Filters

**8.1** On the EXECUTE path, add a filter before the report:

- `confirmation` Equal to `true`
- AND `correlation_conflict` Equal to `false`
- AND `session_valid` Equal to `true`

**8.2** On the fallback (fail) path add Telegram message:
```
⛔ {{pair}} {{direction}} — BLOCKED by Enforcement
Reason: {{reason}}
```

---

# STEP 9 — Report Formatting

**9.1** After Enforcement Filter add **Telegram Bot → Send a Message**:

- Chat ID: `{{1.message.chat.id}}`
- Parse Mode: `Markdown`
- Text:
```
━━━━━━━━━━━━━━━━━━━━
📊 TRADE SIGNAL REPORT
━━━━━━━━━━━━━━━━━━━━
PAIR:      {{pair}} | {{direction}}
GRADE:     {{grade}}
DECISION:  ✅ EXECUTE

📈 ANALYSIS
Trend:        {{trend}}
Structure:    {{structure}}
Zone:         {{zone}}
Liquidity:    {{liquidity}}
Confirmation: {{confirmation}}

💰 RISK
Risk %:    {{risk_percent}}%

📝 REASON
{{reason}}
━━━━━━━━━━━━━━━━━━━━
Reply YES to execute
Reply NO to skip
⏱ Expires in 5 minutes
━━━━━━━━━━━━━━━━━━━━
```

**9.2** Add second Telegram module for your partner using their chat ID.

---

# STEP 10 — Approval Handler Scenario

Create a new scenario: `APFEE — Approval Handler`

**Flow:**
```
Custom Webhook (APFEE Approval Webhook)
→ Router
   → Path 1 (YES): reply Equal to YES
      → Google Sheets Get a Cell (A2)
      → Google Sheets Update a Cell (A2) → {{parseNumber(X.value) + 1}}
      → Google Sheets Get a Cell (B2)
      → Google Sheets Update a Cell (B2) → {{parseNumber(X.value) + 1}}
      → Google Sheets Update a Cell (D2) → {{now}}
      → Telegram confirmation message
      → Telegram partner notification
   → Path 2 (Fallback - NO):
      → Telegram skip message
```

**Confirmation message:**
```
✅ Trade logged successfully!

trades_today updated
Exposure updated
Good luck on the trade! 💰
```

**Skip message:**
```
⏭ Trade skipped.
Waiting for next signal.
```

---

# STEP 11 — Test Scenarios

| # | Test | Signal | Expected Result | Status |
|---|------|--------|----------------|--------|
| 1 | Valid A+ signal | `GBPUSD BUY — London session, clean BOS at 1.2850, FVG fill on M15, liquidity swept, bullish engulfing confirmation` | EXECUTE report sent, A+ grade | ✅ |
| 2 | Non-signal message | `Hey what do you think about gold today` | No response | ✅ |
| 3 | Daily limit (set A2=3) | Valid signal | 🚫 BLOCKED message | ✅ |
| 4 | NO reply | Valid signal → reply NO | ⏭ Skip message, sheet not updated | ✅ |
| 5 | Weak signal | `USDJPY SELL — maybe a setup here not sure` | ⛔ BLOCKED by Claude | ✅ |

---

# STEP 12 — Paper Trading Day

**12.1** Reset Google Sheet — set A2, B2, C2 all to `0`

**12.2** Keep both scenarios **Active**

**12.3** Run for a full London + New York session (7am–9pm UTC)

**12.4** Forward every signal you would normally trade to your bot

**12.5** Reply YES or NO to each report — do NOT open real MT5 trades yet

**12.6** End of day review:
- Did trades_today increment correctly?
- Did all BLOCKs make sense?
- Were any EXECUTEs wrong calls?
- Did last_trade_time update?

**12.7** Fix any issues before going live

---

# STEP 13 — Go Live with Manual MT5

**13.1** Open MetaTrader 5 → log in to prop firm challenge account

**13.2** Keep both Make.com scenarios **Active**

**13.3** When you receive an EXECUTE report:
- Review the signal carefully
- Reply `YES` to execute or `NO` to skip

**13.4** When you reply YES, open the trade in MT5:
- Select the pair and direction from the report
- Calculate lot size: `(Account Balance × risk_percent%) ÷ (SL pips × pip value)`
- Set stop loss at the structural level
- Click **Buy by Market** or **Sell by Market**

**13.5** When trade closes, manually update Google Sheet:
- `open_trades (B2)` subtract 1
- `live_exposure (C2)` subtract risk %
- If loss: `session_losses (E2)` add 1
- If 2 session losses → stop trading, set A2 to 3

---

## ⚠️ Rules You Must Never Break

- Never reply YES to a BLOCKED signal
- Never open a trade that didn't come through the system
- Never exceed risk % limits (0.5% A+, 0.35% A)
- Never trade outside London or New York session
- 2 session losses → stop for the day
- -2% daily drawdown → stop for the day

---

## 🗺️ Phase 2 Roadmap

| Upgrade | Description |
|---------|-------------|
| MT5 Webhook | YES reply auto-triggers MT5 EA to open trade |
| Auto State Update | MT5 EA sends trade result back to Google Sheets |
| Multi-Account | System runs across multiple prop firm accounts |
| Dashboard | Live Google Sheets dashboard (win rate, PnL, grade distribution) |
| Product | Package as a service for other prop traders |

---

## 💻 Tech Stack

| Layer | Tool |
|-------|------|
| Signal Source | Telegram Bot |
| Orchestration | Make.com |
| Decision Engine | Claude API (Anthropic) |
| State Store | Google Sheets |
| Execution | MetaTrader 5 (MT5) |
| Notifications | Telegram |

---

*APFEE v1.0 — Built by Tamir Robertson*
