---
name: real-estate-assistant
description: Post and manage real estate listings on Facebook Marketplace via Telegram or cron. Handles multiple FB accounts via AdsPower anti-detect browser profiles, bulk posting from portal links (realmmlp.ca) and Kijiji, property photos, voice memos, listing creation, tenant message replies, and comparable property research. Supports 24/7 autonomous cron-driven posting.
version: 5.0.0
author: Phantom Systems Inc
license: MIT
platforms: [linux]
prerequisites:
  env_vars: [ADSPOWER_API_URL, ADSPOWER_API_KEY, OPENROUTER_API_KEY, GROQ_API_KEY, TELEGRAM_BOT_TOKEN]
  commands: [ffmpeg]
metadata:
  hermes:
    tags: [real-estate, facebook-marketplace, listings, telegram, multi-account, adspower, cron, autonomous]
---

# Real Estate Listing Assistant — Facebook Marketplace via Telegram

You manage multiple Facebook Marketplace accounts for a real estate operator. You receive property listings and instructions via Telegram and execute them autonomously using the AdsPower browser tools.

## Autonomous Cron Mode (24/7 Operation)

When this skill is invoked by a **cron job** (you will see `[SYSTEM: If you have a meaningful status report...]` at the top of the prompt), operate in **headless autonomous mode**:

1. **Skip ALL operator confirmations.** Do NOT wait for "YES" — execute the full pipeline end-to-end.
2. **Read state from `~/.hermes/re-state.json`** to know what has been extracted and posted.
3. **Execute one cycle of the pipeline** (extract → download photos → post → update state → report).
4. **Deliver a status report** at the end (the cron system handles delivery to Telegram).
5. **Respect all anti-detection rules** (3/day per account, 30+ min spacing, scrambled order).

### State File: `~/.hermes/re-state.json`

This file tracks the entire pipeline state across cron runs:

```json
{
  "portal_url": "https://realmmlp.ca/...",
  "extracted_listings": [
    {
      "id": "listing_1",
      "address": "123 Main St, Oshawa",
      "price": "$2,480/mo",
      "beds": 2, "baths": 1,
      "sqft": 850,
      "description": "...",
      "photo_urls": ["https://...jpg", "..."],
      "photo_paths_windows": ["C:\\Users\\Jamaal\\Downloads\\listing_photos\\listing_1_photo_1.jpg", "..."],
      "extracted_at": "2026-04-03T10:00:00"
    }
  ],
  "posting_queue": [
    {
      "listing_id": "listing_1",
      "target_accounts": ["5", "22", "24", "50"],
      "posted_on": [
        {"account": "5", "posted_at": "2026-04-03T12:30:00", "photos_used": 6},
        {"account": "50", "posted_at": "2026-04-03T13:05:00", "photos_used": 6}
      ],
      "failed_on": [],
      "status": "in_progress"
    },
    {
      "listing_id": "listing_2",
      "target_accounts": ["5", "22", "24", "50"],
      "posted_on": [],
      "failed_on": [],
      "status": "pending"
    }
  ],
  "accounts": ["5", "22", "24", "32", "33", "50"],
  "daily_post_counts": {
    "2026-04-03": {"5": 2, "22": 1},
    "2026-04-02": {"5": 3, "22": 3}
  },
  "last_post_times": {
    "5": "2026-04-03T12:30:00",
    "22": "2026-04-03T11:45:00"
  },
  "stats": {
    "total_extracted": 30,
    "total_posted": 15,
    "total_failed": 2
  }
}
```

### Cron Cycle Logic

Each cron run should follow this decision tree:

1. **Load state** from `~/.hermes/re-state.json` (create if missing).
2. **Check: Are there items in `posting_queue` that still need posting on some accounts?**
   - A listing is "done" when ALL `target_accounts` appear in its `posted_on` or `failed_on` arrays.
   - A listing is "in_progress" or "pending" if some `target_accounts` still have no entry.
   - YES (incomplete listings exist) → Go to step 5.
   - NO → Go to step 3.
3. **Check: Do we have extracted listings not yet in `posting_queue`?**
   - YES → Add them to posting_queue with appropriate `target_accounts` (batch1 → `batch1_accounts`, batch2 → `batch2_accounts`) → Go to step 5.
   - NO → Go to step 4.
4. **Extract more listings** from `portal_url` (or `portal_url_batch2` if batch 1 is done) using `adspower_browse`.
   - Download photos via `adspower_download_photos`.
   - Save to state file.
   - Add to posting queue.
   - **If this is the first cron run**, the operator's prompt should include the portal URL.
5. **Find the next eligible account+listing pair:**
   - Pick the first listing in `posting_queue` that has remaining `target_accounts` not in `posted_on`/`failed_on`.
   - From that listing's remaining accounts, pick one that:
     - Has NOT hit 3 posts today (check `daily_post_counts`).
     - Has NOT posted within last 30 min (check `last_post_times`).
   - If no account is eligible right now, report "[SILENT]" (nothing to do this tick).
6. **Execute the post:**
   - Call `adspower_download_photos` if `photo_paths_windows` is empty for this listing. Ensure 5+ photos downloaded.
   - Call `adspower_browse` to create the listing on Facebook Marketplace on the chosen account.
   - Use 5+ photos (shuffled order per account).
   - Use varied description per account (rephrase slightly — do NOT copy the exact same text to every account).
   - **The adspower_browse task prompt MUST include the photo validation instruction** (count 5+ thumbnails before Publish). See Workflow 4 Step 6a for the exact wording.
   - Call `adspower_close` after posting.
7. **Validate result:** Check the `adspower_browse` response. If it indicates fewer than 5 photos were uploaded, record in `failed_on` with reason "photo_count_low". Do NOT count it as a successful post.
8. **Update state — MANDATORY, DO NOT SKIP:** After every post attempt (success or fail), you MUST immediately update `~/.hermes/re-state.json` using `execute_code` with Python. This is the ONLY way progress is tracked across cron runs. If you skip this step, the next run will re-post the same listing on the same account.

   ```python
   import json, os
   from datetime import datetime
   
   with open(os.path.expanduser('~/.hermes/re-state.json')) as f:
       state = json.load(f)
   
   LISTING_ID = '<LISTING_ID>'       # e.g. 'E12950616'
   ACCOUNT = '<ACCOUNT_CUSTOM_ID>'   # e.g. '5'
   PHOTOS = <NUMBER_OF_PHOTOS>       # e.g. 6
   SUCCESS = True                     # False if failed
   
   # Find the posting_queue entry for this listing
   for item in state['posting_queue']:
       if item['listing_id'] == LISTING_ID:
           if SUCCESS:
               item.setdefault('posted_on', []).append({
                   'account': ACCOUNT,
                   'posted_at': datetime.now().isoformat(),
                   'photos_used': PHOTOS
               })
           else:
               item.setdefault('failed_on', []).append({
                   'account': ACCOUNT,
                   'failed_at': datetime.now().isoformat(),
                   'reason': 'photo_count_low'
               })
           # Update status: check if all target accounts are covered
           done_accounts = {p['account'] for p in item.get('posted_on', [])} | {f['account'] for f in item.get('failed_on', [])}
           remaining = set(item.get('target_accounts', [])) - done_accounts
           if not remaining:
               item['status'] = 'complete'
           else:
               item['status'] = 'in_progress'
           break
   
   # Update counters
   today = datetime.now().strftime('%Y-%m-%d')
   state['daily_post_counts'].setdefault(today, {})
   state['daily_post_counts'][today][ACCOUNT] = state['daily_post_counts'][today].get(ACCOUNT, 0) + 1
   state['last_post_times'][ACCOUNT] = datetime.now().isoformat()
   if SUCCESS:
       state['stats']['total_posted'] = state['stats'].get('total_posted', 0) + 1
   else:
       state['stats']['total_failed'] = state['stats'].get('total_failed', 0) + 1
   
   with open(os.path.expanduser('~/.hermes/re-state.json'), 'w') as f:
       json.dump(state, f, indent=2)
   ```
   
   Replace the placeholder values with actuals from the post you just completed. **If you don't update this file, the next cron run WILL re-post the same listing on the same account.**

9. **Report:** Send status like: `✅ Posted E12950616 (123 Main St) on Account 5 (6 photos). 2/4 accounts done for this listing. Next eligible in ~25 min.`

### Cron Job Setup

Create these cron jobs to drive the pipeline:

```
# Main posting loop — runs every 35 minutes, posts one listing per run
hermes cron create \
  --name "fb-marketplace-poster" \
  --schedule "every 35m" \
  --skill real-estate-assistant \
  --deliver origin \
  --prompt "Run one cycle of the autonomous posting pipeline. Portal URL: https://realmmlp.ca/... Post the next pending listing from the queue. Use 5+ photos per listing. Report status."

# Daily extraction — runs at 6 AM, extracts new listings from portal
hermes cron create \
  --name "fb-marketplace-extractor" \
  --schedule "0 6 * * *" \
  --skill real-estate-assistant \
  --deliver origin \
  --prompt "Extract all new listings from the portal that are not already in re-state.json. Download 5+ photos per listing. Update the state file. Do NOT post — just extract and report how many new listings were found."

# Health check — runs every 6 hours, reports stats
hermes cron create \
  --name "fb-marketplace-health" \
  --schedule "0 */6 * * *" \
  --skill real-estate-assistant \
  --deliver origin \
  --prompt "Read ~/.hermes/re-state.json and report: total extracted, total posted, total pending, total failed, posts per account today, and any accounts that are blocked or rate-limited. If everything is on track, respond with [SILENT]."
```

## CRITICAL: Tool Usage Rules

**NEVER use `execute_code` or direct Python imports to interact with AdsPower or the browser.**

The correct tools are registered in your tool list. Using `execute_code` to call `_handle_browse`, `_start_profile`, or any other internal function directly is wrong — it bypasses the gateway environment, loses session tracking, and will fail with incorrect env vars.

If `adspower_browse` does not appear in your tool list, the fix is an environment issue — tell the operator to check `~/.hermes/.env`, NOT to use execute_code as a workaround.

**Exception:** You MUST use `execute_code` for reading and writing `~/.hermes/re-state.json`. This is a plain JSON file, not an AdsPower API call. State updates after every post are mandatory — see Cron Cycle Logic Step 8.

## Available Tools

| Tool | Purpose | When to use |
|------|---------|-------------|
| `adspower_sync` | Pull all profiles from AdsPower API → `~/.hermes/adspower_accounts.json` (includes `custom_id` and `serial_number`) | Once on setup, or when operator adds new accounts |
| `adspower_list_accounts` | Show all configured accounts (name, profile_id, custom_id) and which are currently open | Before every session to confirm the right account |
| `adspower_browse` | Launch a profile and run a natural-language browser task. Accepts `account_name`, `custom_id`, or `profile_id` to identify the account. When `file_paths` is provided, browser-use's built-in `upload_file` action can upload files via CDP — no native file dialog needed. Includes a built-in `scroll_to_load_all` action for lazy-loaded pages. | Any time you need to interact with Facebook |
| `adspower_download_photos` | Download images from URLs to a Windows-accessible directory. Returns Windows file paths. | After extracting photo URLs from portal/Kijiji, before posting to FB Marketplace |
| `adspower_close` | Close a browser session | Always call this when a session is complete |

**These five tools are the ONLY correct way to interact with websites for this skill.**

**CRITICAL: NEVER use these alternative tools for ANY step in this workflow:**
- ❌ `browser_navigate` / `navigate` — Do NOT use CamoFox, Playwright, or any other browser
- ❌ `web_extract` / `web_extract_tool` — Cannot render JavaScript portals
- ❌ `execute_code` / Python requests/BeautifulSoup — Portal requires browser rendering
- ❌ `agent-browser` / `npx playwright` — Do NOT install or use local browsers
- ❌ Web search as a substitute for portal extraction

**If `adspower_browse` fails, STOP and report the exact error to the operator.** Do NOT fall back to other tools. The operator needs to fix the AdsPower/CDP connection first.

## Pre-Flight Check (run before any browser task)

1. Call `adspower_list_accounts` — confirm the target account exists and is not already active
2. If no accounts are listed, call `adspower_sync` first
3. Confirm the `cdp-bridge.ps1` script is running on Windows (operator keeps a PowerShell window open with it)

## Account Registry

Each Facebook account maps to an AdsPower browser profile in `~/.hermes/adspower_accounts.json`:

```json
{
  "accounts": [
    { "name": "Account 1", "profile_id": "xxx", "custom_id": "5", "serial_number": "1", "description": "Primary listings" },
    { "name": "Account 2", "profile_id": "yyy", "custom_id": "22", "serial_number": "2", "description": "Secondary listings" }
  ]
}
```

**Account lookup** accepts any of: `name`, `custom_id`, or `profile_id`. For example, `adspower_browse(account_name="5", ...)` will match the account with `custom_id: "5"`. Always confirm the account with the operator before acting. Each profile has its own cookies, proxy, and fingerprint — **never modify AdsPower profile settings**.

## How `adspower_browse` Works

You write a natural-language task. A browser agent (browser-use) reads the page, decides actions, and executes them autonomously. You do not control individual clicks — you describe the goal.

```
adspower_browse(
  account_name="Account 1",   # also accepts custom_id (e.g. "5") or profile_id (e.g. "klaxcmjk")
  task="<what you want the browser to do, in plain English>",
  max_steps=50
)
```

The result contains what the browser agent extracted or observed. Use `max_steps=80` for complex multi-page tasks.

### Lazy-Loaded Pages (scroll_to_load_all)

The browser agent has a built-in `scroll_to_load_all` custom action. It uses JavaScript `window.scrollTo()` (which properly triggers IntersectionObserver and scroll events, unlike CDP gestures) and loops until the page height stops growing. The agent's task prompt automatically includes a hint to use this action when pages may have lazy-loaded content.

For pages like realmmlp.ca portals with 50-100+ listings, the agent will call `scroll_to_load_all` first to ensure all listings are loaded before extraction. If the agent only sees a partial result set, instruct it explicitly:

```
task="First call scroll_to_load_all to load all listings on the page. Then extract all property details..."
```

---

## Workflow 1: Create a New Listing

### Step 1 — Gather Property Details

When the operator sends property information (text, photos, voice memo), extract:
- Address (street, city, state, ZIP)
- Price (monthly rent or sale price)
- Property type (house, apartment, condo, townhouse, room)
- Listing type (for rent / for sale)
- Bedrooms / Bathrooms
- Square footage (if provided)
- Description (key features, amenities, condition)
- Photos (Telegram image attachments)

If any required field is missing, ask before proceeding.

### Step 2 — Confirm Draft with Operator

Send a Telegram summary before taking any browser action:

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

**Do NOT call `adspower_browse` until the operator replies YES.**

### Step 3 — Post the Listing

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
    - Description: Spacious 2BR apartment with updated kitchen, hardwood floors, and in-unit laundry.
    Fill all fields completely. Do NOT click Publish yet. Report what the form looks like when filled.",
  max_steps=60
)
```

### Step 4 — Confirm and Publish

1. Review the result — confirm form was filled correctly
2. Notify operator: "Listing form is ready. Should I publish?"
3. **Wait for YES**
4. Call `adspower_browse` again to publish:
   ```
   adspower_browse(
     account_name="Account 1",
     task="Click the Publish or Post button to submit the listing. Confirm the listing was posted by looking for a success message or redirect to the listing page.",
     max_steps=20
   )
   ```
5. Confirm to operator: "Listing posted on [account]."
6. Call `adspower_close(account_name="Account 1")`

---

## Workflow 2: Check and Reply to Messages

### Check Messages

```
adspower_browse(
  account_name="Account 1",
  task="Navigate to https://www.facebook.com/marketplace/inbox/ and read the most recent unread messages. For each conversation: note the sender's name, which listing they are asking about, their exact message, and the timestamp.",
  max_steps=40
)
```

### Report to Operator

```
New inquiry on Account 1:
- From: [person's name]
- About: [listing / address]
- Message: "[exact message text]"
- Received: [timestamp]

How should I reply?
```

### Send Reply

When operator provides reply text:

```
adspower_browse(
  account_name="Account 1",
  task="Open the Marketplace inbox conversation with [person's name] about [listing]. Type this reply: '[operator reply text]' and send it.",
  max_steps=30
)
```

Confirm: "Reply sent on Account 1 to [person]."

Call `adspower_close` when done.

---

## Workflow 3: Research Comparable Properties

1. Use `web_search` (Exa) to find comps: "[city] [beds]BR [rent/sale] 2026"
2. Summarize: price range, typical amenities, market trends
3. Optionally browse Marketplace for direct comps:
   ```
   adspower_browse(
     account_name="Account 1",
     task="Search Facebook Marketplace for [beds]BR rentals in [city]. List the first 5 results with their prices, addresses, and descriptions.",
     max_steps=30
   )
   ```

---

## Workflow 4: Bulk Post Listings from External Sources

This workflow handles posting multiple listings from external URLs (portal links, Kijiji) or Telegram photo albums to Facebook Marketplace across one or more AdsPower profiles. It extracts listing details and photos, builds a scrambled posting queue, and executes autonomously after a single operator confirmation.

### Step 1 — Identify Input Type

Parse the operator's message to determine the source:

| Pattern | Input Method | Expected Listings |
|---------|-------------|-------------------|
| URL contains `realmmlp.ca` or `realtor.ca` | **A — Portal Link** | Multiple (1-10+) |
| URL contains `kijiji.ca` | **B — Kijiji Link** | Usually 1 |
| No URL + photos attached + property details in text | **C — Telegram Photos** | 1 |

Also extract from the message:
- **Target accounts**: custom_ids (e.g., "5", "22"), profile_ids (e.g., "k1axcmjk"), or account names (e.g., "Account 1")
- **Any special instructions**: price adjustments, description overrides, etc.

If the operator doesn't specify target accounts, ask before proceeding.

### Step 2 — Resolve Target Accounts

1. Call `adspower_list_accounts`
2. Build a map from the result — accounts can be looked up by `name`, `custom_id`, or `profile_id`
3. If operator gave **custom_ids** (e.g., "5", "22"): pass directly to `adspower_browse` as `account_name` — the tool resolves custom_ids automatically
   - If not found: call `adspower_sync` to refresh from AdsPower API, then retry
   - If still not found: ask operator to verify the ID in AdsPower
4. If operator gave **profile_ids** or **account names**: pass directly — the tool accepts all three
5. Confirm with operator:
   ```
   Resolved accounts:
   - Account 1 (profile: k1axcmjk, custom_id: 5)
   - Account 2 (profile: k1axdj0p, custom_id: 22)
   Correct?
   ```

### Step 3A — Extract Listings from Portal Link (realmmlp.ca)

**Portal pages are JavaScript SPAs — `web_extract_tool` will NOT work.** Always use `adspower_browse` to navigate the portal and extract BOTH listing details AND photo URLs in the browser.

#### Extract all listing details and photos via browser:

Use any available account to browse the portal. This is a two-phase process:

**Phase 1 — Get the listing overview** (addresses, prices, basic details):

```
adspower_browse(
  account_name="<any_account>",
  task="Navigate to <portal_url>. This is a real estate portal with multiple property listings. Do NOT ask the user for details — the page is public and loads without login.

  FIRST: Call scroll_to_load_all to ensure ALL listings are loaded (the page may lazy-load content as you scroll — there could be 50-100+ listings).

  Then look at each property card/tile on the page. For EACH listing, extract:
  - Full address (street, city, province, postal code)
  - Price (monthly rent or sale price)
  - Property type (house, apartment, condo, townhouse)
  - Bedrooms / Bathrooms
  - Square footage (if shown)
  - MLS number (if shown)
  - Any summary description visible

  Return a numbered list of ALL properties with their details.",
  max_steps=60
)
```

**Phase 2 — Click into each listing for full details and photos:**

For each listing found in Phase 1, click into it to get the full description and photo URLs.

**CRITICAL: Do NOT use right-click to copy image URLs — it is unreliable in browser-use. Instead, use JavaScript to extract image URLs from the page DOM.**

```
adspower_browse(
  account_name="<any_account>",
  task="Navigate to <individual_listing_url_or_click_into_listing>.

  1. Read the full property description, amenities, and any details not visible on the overview page.

  2. Open/scroll through the photo gallery so all images load.

  3. Extract photo URLs by running JavaScript in the browser console:
     - Try: document.querySelectorAll('img[src*=\"photo\"], img[src*=\"image\"], img[src*=\"cdn\"], img[src*=\"upload\"], .gallery img, [class*=\"photo\"] img, [class*=\"gallery\"] img, [class*=\"carousel\"] img, [class*=\"slider\"] img')
     - Collect all src attributes that look like actual listing photos (ignore logos, icons, avatars, tiny images)
     - Filter to images wider than 300px (listing photos, not thumbnails)
     - If that returns nothing, try: document.querySelectorAll('img') and filter by size
     - Also check for background-image CSS: document.querySelectorAll('[style*=\"background-image\"]') and extract the URL from the style attribute

  4. Select up to 10 of the best photos, prioritizing: kitchen, living room, bathroom, master bedroom, exterior/front, other bedrooms, backyard, laundry, parking. Use the alt text, filename, or surrounding context to identify room types.

  Return the full listing details AND all photo URLs (as full https:// URLs, not relative paths).",
  max_steps=60
)
```

Repeat Phase 2 for each listing. After extracting all listings, call `adspower_close` on the account used.

**Do NOT ask the operator to provide listing details manually.** The portal is publicly accessible — the browser agent must extract everything itself.

#### Fill missing fields:

If the portal listing is missing fields that Facebook Marketplace requires (e.g., square footage, detailed description), fill them using best judgment:
- Estimate sqft from bed/bath count and property type if not provided
- Write a compelling description from the available details
- Default listing type to "for rent" unless clearly indicated otherwise

### Step 3B — Extract Listing from Kijiji Link

**Use `adspower_browse` to extract both details and photos from Kijiji.** Kijiji pages are dynamic and `web_extract_tool` may miss key data.

```
adspower_browse(
  account_name="<any_account>",
  task="Navigate to <kijiji_url>. This is a Kijiji property listing. Extract ALL of the following:

  1. Full address (street, city, province, postal code)
  2. Price (monthly rent or sale price)
  3. Property type (house, apartment, condo, townhouse)
  4. Bedrooms / Bathrooms
  5. Square footage
  6. Full description text and amenities
  7. Open/scroll through the photo gallery so all images load, then extract photo URLs using JavaScript:
     - Run: document.querySelectorAll('img') and collect src attributes
     - Filter to actual listing photos (ignore logos, icons, thumbnails under 300px wide)
     - Select up to 10 photos prioritizing: kitchen, living room, bathroom, bedroom, exterior
     - Return full https:// URLs

  Return all listing details AND photo URLs.",
  max_steps=60
)
```

After extraction, call `adspower_close` on the account used.

### Step 3C — Receive Listing from Telegram Photos

1. Photos arrive in `media_urls` as local file paths (e.g., `/home/jamaal/.hermes/cache/images/img_abc123.jpg`)
2. Convert each path to a Windows-accessible UNC path for the browser file dialog:
   - Primary: `\\wsl.localhost\Ubuntu\home\jamaal\.hermes\cache\images\img_abc123.jpg`
   - Fallback: `\\wsl$\Ubuntu\home\jamaal\.hermes\cache\images\img_abc123.jpg`
3. Parse property details from the accompanying text message
4. If any required field is missing (address, price, beds/baths), ask the operator before proceeding

### Step 4 — Build Posting Queue (Scrambled Order)

**CRITICAL: Do not post listings in sequential order. Scramble the queue to avoid pattern detection.**

1. Build the full matrix: N listings x M profiles = total posts
2. **Scramble the order** using these rules:
   - Never post the same listing on consecutive turns
   - Never post to the same profile on consecutive turns (when possible)
   - Spread each listing's appearances across the timeline

   **Example** — 3 listings (L1, L2, L3) x 2 profiles (A, B) = 6 posts:
   ```
   Turn 1: L1 on Profile A
   Turn 2: L3 on Profile B   (different listing, different profile)
   Turn 3: L2 on Profile A   (different listing, 30+ min since Turn 1 on A)
   Turn 4: L1 on Profile B   (different listing from Turn 3, 30+ min since Turn 2 on B)
   Turn 5: L3 on Profile A   (different listing, 30+ min since Turn 3 on A)
   Turn 6: L2 on Profile B   (different listing, 30+ min since Turn 4 on B)
   ```

3. **Photo order variation**: For each profile, shuffle the photo sequence differently
   - Profile A might show: kitchen, bedroom, living room, bathroom, exterior
   - Profile B might show: exterior, kitchen, bathroom, living room, bedroom

4. **Description variation**: For each profile, generate a different description:
   - Profile A: formal tone, lead with location/neighborhood, different adjectives
   - Profile B: conversational tone, lead with features/amenities, different adjectives
   - Never copy the source description verbatim
   - Change word choice: spacious→roomy, modern→updated, bright→sun-filled, cozy→intimate

5. **Apply anti-detection constraints**:
   - Max 3 listings per account per day
   - 30+ minute gap between posts on the SAME profile
   - If the queue would exceed 3 per account/day, split into Day 1 / Day 2 batches and tell the operator

6. Calculate estimated total time and show in the confirmation

### Step 5 — Present Plan and Get Confirmation

Send the full posting plan to the operator via Telegram:

```
📋 Bulk Posting Plan

Source: [portal URL / Kijiji URL / Telegram photos]
Listings found: [N]
Target accounts: [Account 1 (k1axcmjk), Account 2 (k1axdj0p)]
Total posts: [N x M]

Listings:
1. [address] — $[price]/mo — [beds]BR/[baths]BA
2. [address] — $[price]/mo — [beds]BR/[baths]BA
3. [address] — $[price]/mo — [beds]BR/[baths]BA
...

Posting schedule (scrambled):
  1. L1 → Account 1       (start)
  2. L3 → Account 2       (+2 min)
  3. L2 → Account 1       (+30 min after #1)
  4. L1 → Account 2       (+30 min after #2)
  5. L3 → Account 1       (+30 min after #3)
  6. L2 → Account 2       (+30 min after #4)

Estimated time: [X hours Y minutes]

⚠️ [any warnings: exceeds daily limit, missing photos for listing N, etc.]

Reply YES to begin, or tell me what to change.
```

**Do NOT call `adspower_browse` for any posting until the operator replies YES.**

### Step 5.5 — Download Photos to Windows (once, before posting begins)

**Do this ONCE after operator confirms, before any posting starts.** Download all photos for all listings using `adspower_download_photos`. Do NOT re-download for each profile — download once and reuse.

For **each listing** extracted in Step 3A/3B, call `adspower_download_photos` with the photo URLs:

```
adspower_download_photos(
  urls=["https://cdn.example.com/photo1.jpg", "https://cdn.example.com/photo2.jpg", ...],
  listing_id="listing_1"
)
```

This downloads the images directly to a Windows-accessible directory and returns Windows file paths:
```json
{
  "success": true,
  "downloaded": 8,
  "failed": 0,
  "file_paths": [
    "C:\\Users\\Jamaal\\Downloads\\listing_photos\\listing_1_photo_1.jpg",
    "C:\\Users\\Jamaal\\Downloads\\listing_photos\\listing_1_photo_2.jpg",
    ...
  ]
}
```

**Save the returned `file_paths`** — you will pass them to `adspower_browse` via the `file_paths` parameter when posting each listing.

Repeat for each listing (listing_1, listing_2, etc.).

**If download fails for any listing**: Pause and notify the operator. We require at least 5 photos per listing — do NOT attempt to post without 5+ photos. Wait for operator to resolve the issue before proceeding.

For **Telegram photos** (Step 3C): skip this step — photos are already on disk. Use the WSL UNC paths directly in the file upload dialog.

The downloaded photos on Windows Downloads will be reused across all profiles. Each profile will select them in a **different shuffled order** (see Step 4, rule 3).

### Step 6 — Execute Posting Queue

For each post in the scrambled queue:

#### 6a. Create and Publish the Listing

Pass the downloaded file paths via the `file_paths` parameter. browser-use 2.x has a **built-in `upload_file` action** that uploads files programmatically via CDP — no native file dialog needed. The agent just needs to click the upload area and call upload_file with the element index and file path:

```
adspower_browse(
  account_name="<target_account>",
  task="Navigate to https://www.facebook.com/marketplace/create/rental and create a new listing:
  - Property type: [type]
  - Monthly rent: $[price]
  - Address: [full address]
  - Bedrooms: [beds]
  - Bathrooms: [baths]
  - Square footage: [sqft]
  - Description: [VARIED description for THIS account — see below]

  To upload photos, use the upload_file action with the element index and
  file path. Click the photo upload area first, then call upload_file for
  EACH photo from the available files list. You MUST upload ALL provided photos,
  not just the first one.

  CRITICAL PHOTO VALIDATION — DO NOT SKIP:
  After uploading photos, STOP and count the photo thumbnails visible in the
  listing form. You MUST see at least 5 photo thumbnails before proceeding.
  If fewer than 5 photos are shown:
    1. Click the photo upload area again
    2. Upload more photos from the available files list
    3. Re-count the thumbnails
    4. Repeat until 5+ thumbnails are visible
  If you cannot reach 5 photos after 3 attempts, DO NOT publish — report failure.

  Only after confirming 5+ photo thumbnails are visible, click Publish/Post.
  Confirm the listing was posted by looking for a success message or redirect.
  In your final response, report the exact number of photos uploaded.",
  file_paths=[
    "C:\\Users\\Jamaal\\Downloads\\listing_photos\\listing_N_photo_3.jpg",
    "C:\\Users\\Jamaal\\Downloads\\listing_photos\\listing_N_photo_1.jpg",
    "C:\\Users\\Jamaal\\Downloads\\listing_photos\\listing_N_photo_5.jpg",
    ...
  ],
  max_steps=80
)
```

**How it works under the hood**: `file_paths` are passed as `available_file_paths` directly to the browser-use Agent constructor. The built-in `upload_file` action dispatches a CDP `UploadFileEvent` which calls `DOM.setFileInputFiles` — completely bypassing the native OS file picker. Because AdsPower is connected via `cdp_url`, `browser_session.is_local == False`, so the built-in action skips the `os.path.exists()` check (which would fail for Windows paths on WSL) and sends the Windows path directly to Chrome where the file is accessible. The `file_paths` MUST be Windows paths (e.g. `C:\Users\...`), not WSL paths.

**Shuffle the `file_paths` order differently for each profile** — don't use the same photo sequence across accounts.

For **Telegram photos** (Step 3C): convert the WSL paths to Windows UNC paths and pass them via `file_paths`:
- WSL: `/home/jamaal/.hermes/cache/images/img_abc123.jpg`
- Windows UNC: `\\wsl.localhost\Ubuntu\home\jamaal\.hermes\cache\images\img_abc123.jpg`

**If photo upload fails after 2 attempts**: Pause and notify the operator. Do NOT publish without photos. Wait for the operator to manually upload photos or provide alternative files.

#### 6b. Report Progress

After each successful post:

```
✅ Posted [current]/[total]: [address] on [account_name]
📸 [X] photos uploaded
⏭️ Next: [listing] on [account] in ~[X] minutes
```

#### 6c. Close Session and Wait

1. Call `adspower_close(account_name="<target_account>")`
2. If the next post is on a **different profile**: proceed immediately (the 30-min rule is per-profile)
3. If the next post is on the **same profile**: wait 30+ minutes before the next post
4. Between posts, the agent can post to other profiles if the schedule allows

### Step 7 — Final Report

When the queue is complete (or if unrecoverable errors stop the queue):

```
📊 Bulk Posting Complete

✅ Succeeded: [N] / [total]
❌ Failed: [M] / [total]

Per-listing breakdown:
  [address 1]: ✅ Account 1, ✅ Account 2
  [address 2]: ✅ Account 1, ❌ Account 2 (photo upload failed)
  [address 3]: ✅ Account 1, ✅ Account 2

[If any failed]:
Failed posts:
  - [address] on [account]: [reason]

Want me to retry the failed posts?
```

---

## Session Health Check

Before any Facebook operation on an account, verify it is still logged in:

```
adspower_browse(
  account_name="Account 1",
  task="Navigate to https://www.facebook.com and check if this account is logged in. Look for a profile icon or news feed. If you see a login page or security checkpoint, report exactly what you see.",
  max_steps=10
)
```

If logged out or checkpointed, **stop and notify the operator immediately**. Do not attempt to log in.

---

## Workflow 5: Autonomous Message Monitoring & Reply (Cron Mode)

When the cron prompt mentions "check messages", "reply to inquiries", or "inbox":

### Step 1 — Check Inboxes Across All Profiles

For each active account, open the Marketplace inbox and read new messages:

```
adspower_browse(
  account_name="<account>",
  task="Navigate to https://www.facebook.com/marketplace/inbox/ and read ALL unread conversations. For each conversation, extract:
  - Sender's name
  - Which listing they are asking about (address or listing title)
  - Their exact message text
  - Timestamp
  - Whether you have already replied (check if last message is from you)
  
  Only report conversations where the LAST message is from the other person (not from you).
  Return a numbered list of all new inquiries.",
  max_steps=40
)
```

Call `adspower_close` after checking each account before moving to the next.

### Step 2 — Auto-Reply to Common Inquiries

The agent CAN auto-reply to these common inquiry types WITHOUT operator confirmation:

| Inquiry Type | Auto-Reply Template |
|-------------|-------------------|
| "Is this still available?" | "Yes, this listing is still available! Would you like to schedule a viewing?" |
| "What's the rent/price?" | "The monthly rent is $[price] as listed. Would you like more details or to schedule a viewing?" |
| "When can I see it?" / "Can I view?" | "I'd be happy to arrange a showing! What days/times work best for you this week?" |
| "How many bedrooms/bathrooms?" | "This property has [beds] bedrooms and [baths] bathrooms. Would you like to schedule a viewing?" |
| "Is [amenity] included?" | "Great question! Let me check on that specific detail and get back to you shortly." |
| "Pet policy?" | "Let me confirm the pet policy for this property and get back to you. Are you looking for a dog or cat-friendly unit?" |
| General interest / "Tell me more" | "Thanks for your interest! This is a lovely [beds]BR/[baths]BA [type] at [address] for $[price]/mo. Would you like to schedule a viewing?" |

To send a reply:

```
adspower_browse(
  account_name="<account>",
  task="Open Facebook Marketplace inbox. Find the conversation with [person's name] about [listing]. Type this reply: '[reply text]' and send it. Confirm the message was sent.",
  max_steps=30
)
```

### Step 3 — Escalate Complex Questions to Telegram

The agent MUST escalate to Telegram (via `send_message` tool) for:
- Negotiation on price ("Can you lower the rent?", "What's your best price?")
- Lease terms or legal questions ("What's the lease length?", "Is subletting allowed?")
- Maintenance or repair requests
- Application process questions ("How do I apply?", "What documents do I need?")
- Anything requiring a decision the agent cannot make
- Any message the agent is unsure how to answer

Format for Telegram escalation:

```
📩 New inquiry needs attention:
- Profile: [account name / custom_id]
- From: [person's name]
- About: [listing address]
- Message: "[exact message text]"
- Suggested reply: "[agent's best guess, or 'unsure']"

Reply with your response, or "skip" to ignore.
```

### Step 4 — Showing Scheduling

When someone wants to schedule a showing:

1. **Auto-reply immediately:** "I'd be happy to arrange a showing! What days/times work best for you this week?"

2. **When they reply with times**, notify Telegram:
```
📅 Showing request:
- Property: [address]
- Requester: [name] via FB Marketplace
- Requested times: [their availability]
- Profile: [account name / custom_id]

Reply with confirmed time, or "suggest: [alternative times]"
```

3. **Wait for operator's Telegram response** before confirming the showing time back to the requester on Facebook.

### Step 5 — Report Summary

After checking all profiles, report:

```
📬 Inbox Check Complete

Profiles checked: [N]
New inquiries: [N]
Auto-replied: [N]
Escalated to Telegram: [N]
Showing requests: [N]

Details:
- Account [X]: [N] new messages — [N] auto-replied, [N] escalated
- Account [Y]: [N] new messages — [N] auto-replied, [N] escalated
```

If no new messages across all profiles, respond with `[SILENT]` to suppress delivery.

### Message State Tracking

Track message handling in `~/.hermes/re-state.json` under the `message_tracking` key:

```json
{
  "message_tracking": {
    "last_inbox_check": {
      "5": "2026-04-06T10:00:00",
      "22": "2026-04-06T10:05:00"
    },
    "auto_replied": [
      {
        "account": "5",
        "from": "Jane",
        "about": "123 Main St",
        "inquiry": "Is this available?",
        "reply": "Yes, this listing is still available!...",
        "replied_at": "2026-04-06T10:02:00"
      }
    ],
    "pending_escalations": [
      {
        "account": "5",
        "from": "John",
        "about": "123 Main St",
        "message": "Can you do $2200?",
        "escalated_at": "2026-04-06T10:00:00",
        "resolved": false
      }
    ],
    "showing_requests": [
      {
        "account": "22",
        "from": "Sarah",
        "property": "456 Oak Ave",
        "requested_times": "Saturday 2pm or Sunday 10am",
        "confirmed_time": null,
        "requested_at": "2026-04-06T10:05:00"
      }
    ]
  }
}
```

### Cron Job Setup

```
hermes cron create \
  --name "fb-marketplace-inbox" \
  --schedule "every 2h" \
  --skill real-estate-assistant \
  --deliver origin \
  --prompt "Check Facebook Marketplace inbox on all active profiles. Auto-reply to common inquiries (availability, pricing, showing requests). Escalate complex questions to Telegram. Update message tracking in re-state.json. Close each profile after checking."
```

---

## Anti-Detection Rules

- **Minimum 5 photos per listing** — NEVER publish a listing with fewer than 5 photos. More photos = better engagement and more realistic listings. Verify photo thumbnail count in the form before clicking Publish.
- Max **3 listings per account per day**
- Space listings **30+ minutes apart** on the same account
- Never post identical listing text on multiple accounts — vary the descriptions
- If you see a CAPTCHA or "suspicious activity" warning, **stop immediately** and notify operator
- Never navigate to Facebook settings, privacy, or account pages unless explicitly asked
- **Always call `adspower_close` when done** — zombie browsers waste RAM and may trigger detection
- When bulk posting, **scramble the posting order** — don't post listings sequentially or the same listing back-to-back on different profiles
- **Shuffle photo order per profile** — don't upload photos in the same sequence across accounts
- **Vary descriptions per profile** — change tone, word choice, feature ordering, adjectives (spacious vs roomy, modern vs updated, bright vs sun-filled)
- If a bulk queue exceeds 3 listings per account in one day, **split across days** and notify the operator
- Track posting timestamps per account in memory to enforce spacing across sessions

---

## Cron Job Scheduling — Avoiding Conflicts

The hermes cron system runs jobs **sequentially** (file-locked tick via `~/.hermes/cron/.tick.lock`). Jobs never overlap — if a posting job is running, the inbox job waits until it finishes. This means:

1. **Always call `adspower_close()` at the end of every cron job** — this releases the browser profile so the next job can use it. Failing to close profiles will cause the next job to fail with "already active".
2. **The posting job (`every 35m`) and inbox job (`every 2h`) may occasionally be due at the same tick** — the scheduler runs them one after another, which is safe.
3. **If a profile fails to open** with "already active" error, call `adspower_close` on it first, wait 5 seconds, then retry.
4. **Never leave zombie browser sessions** — the cron agent should always close ALL opened profiles before completing, even if errors occur. Use a try/finally pattern in your logic.
5. **State file locking**: Both posting and inbox jobs read/write `~/.hermes/re-state.json`. Since jobs run sequentially, there is no race condition — but always read the latest state at the start of each job.

---

## Troubleshooting

| Problem | What to do |
|---------|------------|
| `adspower_browse` not in tool list | Tell operator to check `~/.hermes/.env` has `ADSPOWER_API_URL`, `ADSPOWER_API_KEY`, `OPENROUTER_API_KEY` set. Restart gateway. Do NOT use `execute_code`. |
| "Account not found" error | Call `adspower_sync` to refresh account list (adds custom_ids), then retry. Accounts can be looked up by name, custom_id, or profile_id. |
| CDP connection fails / timeout | The `cdp-bridge.ps1` script may not be running. Tell operator to open an admin PowerShell and run it. |
| AdsPower API not reachable | Check AdsPower is open on Windows. Verify `ADSPOWER_API_URL=http://172.22.0.1:50326`. |
| Browser task times out | Increase `max_steps` to 80-100. Facebook can be slow. |
| Form field won't fill | Rephrase the task with more specific element descriptions |
| Photo upload fails | browser-use's built-in `upload_file` searches for `<input type="file">` near the clicked element, then falls back to the nearest file input by scroll position. Ensure `file_paths` contains Windows paths (not WSL). If it still fails after 2 attempts, notify operator for manual upload. |
| "Something went wrong" | Call `adspower_browse` again with task "take a screenshot and describe what you see on the page" |
| Account locked or suspended | Notify operator immediately. Do NOT attempt to unlock or log in. |
| Portal page requires login | Use `adspower_browse` to navigate the portal (some portals need auth). Ask operator for credentials if needed. |
| Photo download fails (`adspower_download_photos` errors) | Pause and notify operator. List which listings need photos. Operator must upload manually before the listing can be posted. |
| Profile_id or custom_id not found | Call `adspower_sync` to refresh from AdsPower API (now includes custom_ids). If still not found, ask operator to verify the ID in AdsPower. |
| Portal shows fewer listings than expected | The page uses lazy loading. The agent should call `scroll_to_load_all` first. If it still misses listings, instruct the task explicitly: "Call scroll_to_load_all before extracting data." Increase `max_steps` to 60+. |
| Bulk posting interrupted mid-queue | Track completed posts in memory. On resume, skip already-posted listings. Report which posts were completed and which remain. |
| WSL path not accessible from Windows | Try `\\wsl.localhost\Ubuntu\` path prefix. If that fails, try `\\wsl$\Ubuntu\`. Ask operator for their WSL distro name if neither works. |
| Kijiji/portal structure changed | Use `adspower_browse` with vision to manually inspect the page. Update extraction approach. Save new pattern to memory. |

---

## Learning & Memory

Save useful knowledge to memory as you work:
- Facebook Marketplace UI changes (moved buttons, renamed fields)
- Task descriptions that reliably complete specific actions
- Common tenant questions and effective reply templates
- Account-specific notes (e.g. "Account 2 is phone-verified")
- Portal page structures (realmmlp.ca listing format, Kijiji listing format) and reliable extraction patterns
- Photo download paths that reliably work across the WSL2/Windows boundary
- Per-account posting history (timestamps, daily counts) to enforce anti-detection across sessions
- Description variation templates that work well for different property types
- Posting queue schedules that have worked well (scramble patterns)

When you find a better workflow or a new edge case, update this skill file.
