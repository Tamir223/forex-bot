<<<<<<< HEAD
# 🏗️ Agentic Prop Firm Execution Engine (APFEE)
### Complete Step-by-Step Build Guide

> **Core Principle:** The system makes decisions. The human approves. Execution follows rules only.

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

# STEP 1 — Create Your Telegram Bot

This bot is what receives your signals and sends back decisions.

**1.1** Open Telegram on your phone or desktop

**1.2** In the search bar at the top, type:
```
@BotFather
```

**1.3** Tap on **BotFather** (the one with the blue verified checkmark)

**1.4** Tap **START** at the bottom of the chat

**1.5** Type and send:
```
/newbot
```

**1.6** BotFather will ask: *"Alright, a new bot. How are we going to call it?"*
Type a display name for your bot, for example:
```
APFEE Signal Engine
```

**1.7** BotFather will ask: *"Now let's choose a username for your bot."*
It must end in `bot`. Type something like:
```
APFEESignalBot
```
(If that name is taken, try adding numbers: `APFEESignal1Bot`)

**1.8** BotFather will reply with a message containing your **HTTP API Token**. It looks like this:
```
1234567890:ABCDefGHIJKlmNoPQRsTUVwxyZ
```

**1.9** Copy that token and save it somewhere safe (Notes app, text file). You will need it in Step 2.

**1.10** Go back to your bot by searching its username in Telegram and press **START**

---

# STEP 2 — Create Your Make.com Scenario Skeleton

Make.com is the automation platform that connects everything together.

**2.1** Go to [make.com](https://make.com) and log in

**2.2** In the left sidebar, click **Scenarios**

**2.3** Click the blue **+ Create a new scenario** button (top right)

**2.4** You will see an empty canvas with a large `+` circle in the middle. This is where you build your flow.

**2.5** Click the `+` circle to add your first module

**2.6** In the search box that appears, type:
```
Telegram
```

**2.7** Select **Telegram Bot** from the results

**2.8** Select the trigger: **Watch Updates**

**2.9** Make.com will ask you to create a connection. Click **Add** next to the connection field.

**2.10** A popup will appear asking for your Bot Token. Paste the token you saved in Step 1.8

**2.11** Click **Save**

**2.12** Set the **Limit** field to `1` (this makes it process one message at a time, faster)

**2.13** Click **OK** to confirm the module

**2.14** At the bottom of the screen, click the **Schedule** button (clock icon) and set it to:
- Every: `1 minute`

**2.15** Click **Save** (floppy disk icon, top right). Name your scenario:
```
APFEE — Main Engine
```

---

# STEP 3 — Add Signal Intake and Filter

This step makes sure only real trading signals get processed.

**3.1** Hover over the right edge of the Telegram module. Click the small `+` that appears.

**3.2** Instead of adding a new app module, look for the filter option. Click the **wrench icon** on the arrow between modules.

**3.3** Click **Set up a filter**

**3.4** Name the filter:
```
Signal Keyword Filter
```

**3.5** Set up the condition using OR rules:

- **Condition 1:**
  - Field: `Text` (from the Telegram message)
  - Operator: `Contains`
  - Value: `BUY`

- Click **Add OR rule**

- **Condition 2:**
  - Field: `Text`
  - Operator: `Contains`
  - Value: `SELL`

**3.6** Click **OK** to save the filter

> Any message that does NOT contain BUY or SELL will be dropped here automatically. Nothing else happens.

---

# STEP 4 — Connect Claude API with JSON Prompt

This is the brain of the system. Claude receives the signal and grades it.

**4.1** First, get your Claude API key. Open a new browser tab and go to:
```
https://console.anthropic.com
```

**4.2** Sign up or log in with your email

**4.3** In the left sidebar, click **API Keys**

**4.4** Click **+ Create Key**

**4.5** Name it:
```
APFEE Make.com
```

**4.6** Click **Create Key** and immediately copy the key shown. It starts with `sk-ant-...`
Save it in your Notes app — you cannot view it again after closing.

**4.7** Go back to Make.com. Hover over the right edge of the Signal Filter and click `+`

**4.8** Search for:
```
HTTP
```

**4.9** Select **HTTP** → **Make a Request**

**4.10** Fill in the module fields exactly as follows:

- **URL:**
```
https://api.anthropic.com/v1/messages
```

- **Method:** `POST`

- **Headers** — click **Add item** for each one:

  | Key | Value |
  |-----|-------|
  | `x-api-key` | your `sk-ant-...` key from Step 4.6 |
  | `anthropic-version` | `2023-06-01` |
  | `Content-Type` | `application/json` |

- **Body Type:** `Raw`

- **Content Type:** `JSON (application/json)`

- **Request Content** — paste this exactly:
```json
{
  "model": "claude-sonnet-4-20250514",
  "max_tokens": 1000,
  "system": "You are a prop firm trade filter. Analyze the trading signal and return ONLY a valid JSON object with these exact fields: pair, direction, grade (A+/A/BLOCK), decision (EXECUTE/BLOCK), risk_percent (0.5 for A+, 0.35 for A, 0 for BLOCK), trend, confirmation (true/false), structure (true/false), zone (true/false), liquidity (true/false), retracement_valid (true/false), correlation_conflict (true/false), session_valid (true/false), reason. Grade A+ only for perfect setups with trend alignment, clear structure, valid zone, liquidity sweep, and confirmation. Grade A for good setups missing one minor element. BLOCK everything else.",
  "messages": [
    {
      "role": "user",
      "content": "{{1.message.text}}"
    }
  ]
}
```

> Note: `{{1.message.text}}` pulls the signal text from Module 1. If Make.com shows a different number, click the field and pick the text variable from the dropdown.

**4.11** Click **OK**

---

# STEP 5 — Parse Claude's Output and Route on Decision

Claude returns a JSON string. This step reads it and splits the flow.

**5.1** Click `+` after the HTTP module

**5.2** Search for:
```
JSON
```

**5.3** Select **JSON** → **Parse JSON**

**5.4** In the **JSON String** field, click it and select from the HTTP module output:
- `Data` → `Body`

**5.5** Under **Data Structure**, click **Generate**

**5.6** Paste this sample JSON to auto-generate the schema:
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
  "reason": "Clean BOS with FVG fill and liquidity sweep"
}
```

**5.7** Click **Save** on the data structure popup

**5.8** Click **OK** to close the module

**5.9** Now add a Router after the Parse JSON module:
- Click `+` → search **Flow Control** → select **Router**

**5.10** The router creates two parallel paths. Set up filters on each:

- Click the **filter icon** on Path 1:
  - Label: `EXECUTE`
  - Condition: `decision` — Text operator — `Equal to` — `EXECUTE`

- Click the **filter icon** on Path 2:
  - Label: `BLOCK`
  - Condition: `decision` — Text operator — `Equal to` — `BLOCK`

**5.11** On the BLOCK path, add a **Telegram Bot → Send a Message** module:
- Chat ID: `{{1.message.chat.id}}`
- Text:
```
⛔ {{pair}} {{direction}} — BLOCKED
Reason: {{reason}}
```

---

# STEP 6 — Add Fast Gate Filters

These gates run BEFORE Claude to block obvious violations instantly.

**6.1** In your scenario, click on the path between the Signal Filter (Step 3) and the HTTP Claude module (Step 4)

**6.2** You need to read state values from Google Sheets first (set that up in Step 7, then come back to wire these filters). For now, create the filter structure:

**6.3** Add a **Filter** on that path — Name it `Fast Gate: Session`

Set condition:
- Use Make's built-in time functions
- Condition 1: `{{formatDate(now; "HH:mm")}}` greater than or equal to `07:00` AND less than or equal to `16:00`
- OR
- Condition 2: `{{formatDate(now; "HH:mm")}}` greater than or equal to `12:00` AND less than or equal to `21:00`

**6.4** Add another **Filter** — Name it `Fast Gate: Trade Count`
- Condition: `trades_today` (from Google Sheets) less than `3`

**6.5** Add another **Filter** — Name it `Fast Gate: Open Trades`
- Condition: `open_trades` less than `2`

**6.6** Add another **Filter** — Name it `Fast Gate: Exposure`
- Condition: `live_exposure` less than `1`

**6.7** Add another **Filter** — Name it `Fast Gate: Spacing`
- Condition: `{{dateDifference(now; last_trade_time; "minutes")}}` greater than `10`

**6.8** If ANY gate fails, add a **Telegram → Send a Message** at the failure exit:
```
🚫 Signal blocked — gate violation
Check: session / trade count / exposure / spacing
```

---

# STEP 7 — Add State Tracking with Google Sheets

This is your system's memory. It tracks everything between trades.

**7.1** Go to [sheets.google.com](https://sheets.google.com) and create a new spreadsheet

**7.2** Name it:
```
APFEE State Store
```

**7.3** In the first sheet (rename it `State`), enter these exact headers in Row 1:

| A | B | C | D | E | F | G | H | I |
|---|---|---|---|---|---|---|---|---|
| trades_today | open_trades | live_exposure | last_trade_time | session_losses | win_streak | daily_pnl | challenge_mode | endgame_mode |

**7.4** In Row 2, enter these starting values:
```
0 | 0 | 0 | (leave blank) | 0 | 0 | 0 | TRUE | FALSE
```

**7.5** Go back to Make.com. Before your fast gate filters, add a new module at the very beginning of the EXECUTE flow:
- Click `+` → search `Google Sheets`
- Select **Google Sheets** → **Get a Row**

**7.6** Connect your Google account:
- Click **Add** → sign in with Google → click **Allow**

**7.7** Configure the module:
- **Spreadsheet:** Select `APFEE State Store`
- **Sheet:** Select `State`
- **Row Number:** `2`

**7.8** Click **OK**. All 9 columns now flow out of this module as variables you can use in your filters.

**7.9** Go back to Step 6 and wire the filter conditions to use these variables:
- `trades_today` → Column A output from this module
- `open_trades` → Column B
- `live_exposure` → Column C
- `last_trade_time` → Column D
- `session_losses` → Column E

---

# STEP 8 — Add Enforcement Filters

These run AFTER Claude as a second layer of protection.

**8.1** On the EXECUTE path (Path 1 from Step 5.10), add a **Filter** before the report module:

- Name: `Enforcement Gate`

**8.2** Add these AND conditions (all must be TRUE to proceed):

- Condition 1: `correlation_conflict` — Boolean — `Equal to` — `false`
- Click **Add AND rule**
- Condition 2: `confirmation` — Boolean — `Equal to` — `true`
- Click **Add AND rule**
- Condition 3: `session_valid` — Boolean — `Equal to` — `true`

**8.3** Add a **Telegram → Send a Message** at the failure exit of this filter:
```
⛔ {{pair}} {{direction}} — BLOCKED by Enforcement
Reason: {{reason}}
```

---

# STEP 9 — Add Report Formatting

This is the message you receive on Telegram asking for your decision.

**9.1** After the Enforcement Filter, add a new module:
- Click `+` → search `Telegram Bot`
- Select **Telegram Bot** → **Send a Message**

**9.2** Use the same bot connection from Step 2

**9.3** In the **Chat ID** field: `{{1.message.chat.id}}`

**9.4** In the **Text** field, paste this exactly:
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

**9.5** Under **Parse Mode**, select `Markdown`

**9.6** Click **OK**

---

# STEP 10 — Add the Approval Loop

The system waits for your YES or NO before doing anything.

**10.1** After the Send Report module, add a new module:
- Click `+` → search `Telegram Bot`
- Select **Telegram Bot** → **Watch Updates**

**10.2** Use the same bot connection

**10.3** Add a **Router** after this second Watch Updates module with two paths:

**Path 1 — YES:**
- Filter condition: `message.text` equals `YES`
- This path continues to execution (Step 11)

**Path 2 — NO:**
- Filter condition: `message.text` equals `NO`
- Add a **Telegram → Send a Message**:
```
⏭ Trade skipped. Waiting for next signal.
```

**10.4** For expired trades, add a **Tools → Sleep** module before the Watch Updates:
- Duration: `300` seconds (5 minutes)
- After sleep, if no reply: send Telegram message:
```
⏰ Trade expired — no response within 5 minutes. Signal invalidated.
```

---

# STEP 11 — Run Test Scenarios

Before going live, test every scenario manually.

**11.1** In Make.com, click **Run once** (bottom left play button). This runs the scenario for one single cycle so you can test safely.

---

**Test 1 — Valid Signal Should Execute:**

Send this exact message to your bot:
```
GBPUSD BUY — London session, clean BOS above 1.2850, FVG fill on M15, liquidity swept at 1.2820, bullish engulfing confirmation
```
✅ Expected: Full report sent to Telegram with EXECUTE

---

**Test 2 — Invalid Signal Should Be Dropped:**

Send this to your bot:
```
Hey what do you think about gold today
```
✅ Expected: Nothing happens — dropped at the keyword filter

---

**Test 3 — Outside Session Should Block:**

Temporarily edit your session filter times to a window that excludes your current time, then send:
```
EURUSD SELL — valid setup
```
✅ Expected: BLOCKED — session violation message received

Reset your filter times after testing.

---

**Test 4 — Trade Limit Should Block:**

In your Google Sheet, manually set `trades_today` (Column A, Row 2) to `3`, then send a valid signal.

✅ Expected: BLOCKED — daily limit reached

Reset `trades_today` to `0` after testing.

---

**Test 5 — No Confirmation Should Block:**

Send a vague signal with no confirmation details:
```
USDJPY SELL — maybe a setup here, not sure
```
✅ Expected: Claude returns `confirmation: false` → BLOCKED by Enforcement Gate

---

# STEP 12 — Run a Full Paper Trading Day

Test the system with real signals but no real money.

**12.1** Toggle your Make.com scenario ON (the toggle at the bottom left turns blue)

**12.2** Run it for a full London + New York session (7:00am – 9:00pm UTC)

**12.3** Forward every signal you would normally trade to your bot in Telegram

**12.4** Reply YES or NO to each approved signal as you normally would — but **do NOT open any real trades in MT5 yet**

**12.5** At end of day, open your Google Sheet and check:
- Did `trades_today` increment correctly?
- Did `live_exposure` update after each YES?
- Did `last_trade_time` update?
- Were all BLOCKs the right calls?
- Were any EXECUTEs wrong setups?

**12.6** Fix anything that behaved incorrectly in Make.com before going live

---

# STEP 13 — Go Live with Manual MT5 Execution

You are ready. Real signals, real money, system is live.

**13.1** Open MetaTrader 5 on your desktop and log in to your prop firm challenge account

**13.2** Toggle your Make.com scenario ON

**13.3** When a valid signal comes through and you receive the EXECUTE report on Telegram:
- Read the report carefully
- If you agree with the setup → reply `YES`
- If you want to skip → reply `NO`

**13.4** When you reply YES, immediately open the trade in MT5:
- Select the pair shown in the report
- Set direction (BUY or SELL as shown)
- Calculate your lot size:
  - Formula: `(Account Balance × risk_percent%) ÷ (Stop Loss in pips × pip value)`
  - Example: $10,000 account × 0.5% = $50 risk
- Set your stop loss at the structural level mentioned in the reason
- Click **Buy by Market** or **Sell by Market**

**13.5** After opening the trade, manually update your Google Sheet:
- `open_trades` → change to current number of open positions
- `live_exposure` → add the risk % of the new trade
- `last_trade_time` → enter current time
- `trades_today` → add 1

**13.6** When the trade closes (win or loss), manually update Google Sheet:
- `open_trades` → subtract 1
- `live_exposure` → subtract the closed trade's risk %
- If loss: `session_losses` → add 1
- If 2 session losses → stop trading for the day, set `trades_today` to 3
- Update `win_streak` and `daily_pnl`

---

## ⚠️ Rules You Must Never Break

- Never reply YES to a signal that was BLOCKED
- Never open a trade MT5 did not come from this system
- Never increase risk % above the grade limits (0.5% A+, 0.35% A)
- Never trade outside London or New York session
- If you hit 2 losses in a session — stop for the day immediately
- If you hit -2% daily drawdown — stop for the day immediately

---

## 🗺️ Phase 2 Roadmap (After Challenge Passed)

| Upgrade | Description |
|---------|-------------|
| MT5 Webhook | YES reply automatically triggers MT5 EA to open the trade |
| Auto State Update | MT5 EA sends trade result back to Google Sheets automatically |
| Multi-Account | Same system runs across multiple prop firm accounts simultaneously |
| Dashboard | Google Sheets live dashboard showing win rate, PnL, grade distribution |
| Product | Package and sell as a managed service to other prop traders |

---

*APFEE v1.0 — Built by Tamir*
=======
# prop-firm-engine
>>>>>>> 72036321f9039318b81e37c6e29ecf7ddf4ecc80
