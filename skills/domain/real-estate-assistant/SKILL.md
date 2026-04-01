---
name: real-estate-assistant
description: Post and manage real estate listings on Facebook Marketplace via Telegram. Handles multiple FB accounts via AdsPower anti-detect browser profiles, property photos, voice memos, listing creation, tenant message replies, and comparable property research.
version: 2.0.0
author: Phantom Systems Inc
license: MIT
platforms: [linux]
prerequisites:
  env_vars: [ADSPOWER_API_URL, OPENROUTER_API_KEY, GROQ_API_KEY, TELEGRAM_BOT_TOKEN]
  commands: [ffmpeg]
metadata:
  hermes:
    tags: [real-estate, facebook-marketplace, listings, telegram, multi-account, adspower]
---

# Real Estate Listing Assistant — Facebook Marketplace via Telegram

This skill enables you to create, manage, and optimize property listings on Facebook Marketplace. You receive instructions via Telegram (text, photos, voice memos) and execute them by automating Facebook Marketplace through AdsPower anti-detect browser profiles controlled by browser-use.

## Account Registry

You manage multiple Facebook accounts. Each account maps to an AdsPower browser profile configured in `~/.hermes/adspower_accounts.json`:

| Account Alias | AdsPower Profile ID | Purpose |
|---------------|---------------------|---------|
| Account 1     | (configured in JSON) | Primary listings account |
| Account 2     | (configured in JSON) | Secondary listings account |

When the operator adds more accounts, they update `adspower_accounts.json`. The operator may refer to accounts by alias, number, or ask you to rotate. Always confirm which account before taking action.

Each AdsPower profile has its own cookies, proxy, fingerprint, and user-agent — fully isolated. **Never modify these settings.** They are pre-configured in the AdsPower desktop app.

## How Browser Automation Works

You have 3 tools for browser automation:

1. **`adspower_list_accounts`** — See available accounts and which are active
2. **`adspower_browse`** — Launch a profile and run a browser task autonomously. Give it a natural language task description and it handles all the clicking, typing, and navigation.
3. **`adspower_close`** — Close a browser session when done (frees RAM on Windows host)

The `adspower_browse` tool uses a browser agent (browser-use) that interprets pages and decides actions. You give it high-level instructions; it handles the details.

## Workflow 1: Create a New Listing

### Step 1: Gather Property Details

When the operator sends property information (text, photos, and/or voice memo), extract:
- **Address** (street, city, state, ZIP)
- **Price** (monthly rent or sale price)
- **Property type** (house, apartment, condo, townhouse, room)
- **Listing type** (for rent or for sale)
- **Bedrooms / Bathrooms**
- **Square footage** (if provided)
- **Description** (key features, amenities, condition)
- **Photos** (Telegram image attachments — cached locally)

If any required field is missing, ask the operator before proceeding.

### Step 2: Confirm Listing Draft

Send a summary to the operator on Telegram:

```
Listing Draft:
- Account: [alias]
- Title: [descriptive title]
- Price: $X,XXX/mo
- Location: [full address]
- Type: [property type] for [rent/sale]
- Beds/Baths: X / X
- Sqft: X,XXX (if known)
- Description: [2-3 sentences]
- Photos: [X photos ready]

Ready to post? Reply YES to confirm or tell me what to change.
```

**Do NOT proceed until the operator replies YES.**

### Step 3: Post the Listing

Use `adspower_browse` with the account name and a detailed task:

```
adspower_browse(
  account_name="Account 1",
  task="Navigate to https://www.facebook.com/marketplace/create/rental and create a new rental listing with these details:
    - Property type: Apartment
    - Monthly rent: $1,500
    - Address: 123 Main St, Springfield, IL 62701
    - Bedrooms: 2
    - Bathrooms: 1
    - Square footage: 850
    - Description: Spacious 2BR apartment with updated kitchen, hardwood floors, and in-unit laundry. Close to downtown and public transit.
    
    Fill in all fields, then scroll down to review. Do NOT click Publish yet — take a screenshot of the completed form.",
  max_steps=50
)
```

### Step 4: Review and Submit

1. Review the result from `adspower_browse` to check if the form was filled correctly
2. Send the status to the operator: "Listing form is filled. Should I publish?"
3. **Wait for operator confirmation**
4. Run another `adspower_browse` task to click Publish:
   ```
   adspower_browse(
     account_name="Account 1",
     task="Click the Publish or Post button to submit the listing. Verify the listing was posted successfully by checking for a success message or redirect to the listing page."
   )
   ```
5. Send confirmation to operator: "Listing posted successfully on [account]."
6. **Always call `adspower_close` when done** to free the browser on Windows.

## Workflow 2: Reply to Tenant/Buyer Messages

### Check Messages

```
adspower_browse(
  account_name="Account 1",
  task="Navigate to https://www.facebook.com/marketplace/inbox/ and read the most recent unread messages. For each message, note: the sender's name, which listing they're asking about, their exact message text, and the timestamp."
)
```

### Report to Operator

Send a summary on Telegram:

```
New inquiry on [account]:
- From: [person's name]
- About: [listing title / address]
- Message: "[their exact message]"
- Received: [timestamp if visible]

How should I reply?
```

### Send Reply

When the operator provides a reply:
```
adspower_browse(
  account_name="Account 1",
  task="Go to the Marketplace inbox conversation with [person's name] about [listing]. Type this reply: '[operator's reply text]' and send it."
)
```

Confirm: "Reply sent on [account] to [person]."

## Workflow 3: Research Comparable Properties

When asked to research comps or market rates:
1. Use `web_search` (Exa) to find comparable listings in the area
2. Search queries like: "[city] [beds]BR rental price 2026", "[neighborhood] apartments for rent"
3. Summarize findings: price range, typical amenities, market trends
4. Optionally browse other Facebook Marketplace listings for direct comps using `adspower_browse`

## Rate Limiting & Anti-Detection

**Critical rules to avoid account bans:**
- Maximum **3 listings per account per day**
- Space listings at least **30 minutes apart**
- Never post the same listing text on multiple accounts (vary descriptions)
- The browser-use agent already types at human-like speed; do not override this
- If you encounter a CAPTCHA or "suspicious activity" warning, **STOP immediately** and notify the operator
- Never navigate to Facebook settings, privacy, or account pages unless explicitly asked
- Avoid rapid page refreshing or back-and-forth navigation
- **Always close sessions when done** — zombie browsers waste RAM and may look suspicious

## Session Health Check

Before starting any Facebook operation:
```
adspower_browse(
  account_name="Account 1",
  task="Navigate to https://www.facebook.com and check if this account is logged in. Look for a profile icon, news feed, or marketplace link. If you see a login page or security checkpoint, report what you see."
)
```

If the session is expired or locked, notify the operator immediately.

## Learning & Self-Improvement

As you use this skill, save useful knowledge to memory:
- Facebook Marketplace UI changes (moved buttons, new form fields)
- Listing description templates that work well
- Common tenant questions and effective reply templates
- Browser task descriptions that reliably work with browser-use
- Account-specific notes (e.g., "Account 2 is verified, gets more visibility")

When you discover a better workflow or encounter a new edge case, consider updating this skill file to capture the improvement.

## Troubleshooting

| Problem | Action |
|---------|--------|
| AdsPower not reachable | Check that AdsPower is running on Windows. Verify `ADSPOWER_API_URL` in `.env`. |
| Browser task times out | Increase `max_steps`. Facebook can be slow — 50-80 steps is typical. |
| Can't fill a form field | Describe what the browser agent sees and ask operator for guidance. |
| Photo upload fails | Notify operator, suggest manual upload as workaround. |
| "Something went wrong" error | Run another browse task to screenshot the page, send to operator. |
| Account locked/suspended | Notify operator immediately. Do NOT attempt to unlock. |
| Listing removed by Facebook | Notify operator with details. Do not re-post without instruction. |
| CDP connection fails | The WebSocket port may be blocked by Windows Firewall. Ask operator to check. |
