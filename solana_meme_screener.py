"""
Solana Memecoin Screener + Telegram Alert Bot
================================================

What it does
------------
1. Polls DexScreener for recently active Solana pairs matching your
   liquidity/volume filters.
2. For each new token it hasn't seen before, pulls a risk report from
   RugCheck.xyz (liquidity lock %, mint/freeze authority, holder
   concentration, insider/bundle clusters).
3. Scores the token against the criteria you asked about (liquidity,
   bundling, whale concentration, dev holdings).
4. Sends a Telegram alert for anything that clears your score threshold.

IMPORTANT — read before relying on this
----------------------------------------
- This is a heuristic screener, not a guarantee. Rug pullers actively
  adapt to get around tools like this. Never treat a "PASS" as
  financial advice or a reason to skip your own judgement.
- I could not test live network calls while building this (my sandbox
  has no internet access). DexScreener's and RugCheck's public API
  shapes are documented and have been stable, but double check the
  response JSON the first time you run it (print(resp.json()) is your
  friend) in case a field name has changed.
- RugCheck's public endpoint is unauthenticated for basic reports but
  is rate-limited. If you outgrow it, Birdeye and Helius both offer
  paid APIs with richer holder/sniper data.
- You are responsible for your own trading decisions and risk.

Setup
-----
1. pip install requests
2. Create a Telegram bot via @BotFather, copy the token.
3. Message your bot once, then get your chat_id from:
   https://api.telegram.org/bot<TOKEN>/getUpdates
4. Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID below (or set as
   environment variables).
5. Run: python solana_meme_screener.py

Optional — Arkham auto-labeling
--------------------------------
By default, KOL/smart-money detection only matches wallets you manually
add to KNOWN_KOL_WALLETS. To auto-label wallets using Arkham
Intelligence's entity database instead:
1. Get an API key at https://intel.arkm.com/api (paid — check current
   pricing/rate limits, they change).
2. Set ARKHAM_ENABLED = True and ARKHAM_API_KEY (or env var).
3. It'll check the creator wallet + top N holders per token against
   Arkham automatically, alongside your manual list.
"""

import os
import time
import json
import requests

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

POLL_INTERVAL_SECONDS = 60          # how often to check for new tokens (only matters in loop mode)
SEEN_TOKENS_FILE = "seen_tokens.json"

# Set RUN_ONCE=true (env var) when running under a scheduler like GitHub
# Actions or cron, where the scheduler itself controls timing — the script
# does one poll pass and exits instead of looping forever. Leave false for
# a VPS/Railway-style always-on process.
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() == "true"

# Screening thresholds — tune these to your risk appetite
MIN_LIQUIDITY_USD = 8_000           # below this, slippage/rug risk is high
MIN_LIQUIDITY_LOCKED_PCT = 100      # % of LP locked or burned — max, requires fully locked/burned
MAX_TOP10_HOLDER_PCT = 40           # top 10 wallets shouldn't own more than this
MAX_SINGLE_HOLDER_PCT = 10          # excluding LP/burn address
MAX_DEV_HOLDING_PCT = 5             # creator/dev wallet specifically must hold <= this
MIN_MARKET_CAP_USD = 10_000         # below this, too early/illiquid to trust the data
MIN_VOLUME_24H_USD = 50_000         # 24h trading volume floor — filters out dead/no-interest tokens
MIN_HOLDER_COUNT = 50               # total distinct holders
REQUIRE_MINT_AUTHORITY_REVOKED = True
REQUIRE_FREEZE_AUTHORITY_REVOKED = True
MIN_SCORE_TO_ALERT = 70             # out of 100, see score_token()
REQUIRE_X_SOCIAL = True             # skip tokens with no linked X/Twitter account

# Known KOL / smart-money wallet addresses to watch for.
# There's no reliable free API that labels "this is a KOL wallet" — that
# data comes from paid platforms like GMGN.ai or Arkham Intelligence, or
# from your own tracking of which wallets known influencers trade from.
# Fill this in yourself; format is {wallet_address: "display name"}.
KNOWN_KOL_WALLETS = {
    # "3xamPLewaLLetADDRessHERE1111111111111111": "Example KOL name",
}
KOL_SCORE_BOOST = 25                # added to score if a KOL wallet is involved
KOL_ALWAYS_ALERTS = True            # alert on KOL involvement even if below MIN_SCORE_TO_ALERT

# Arkham Intelligence auto-labeling (optional, paid API key required).
# When enabled, the bot looks up the creator wallet + top holders against
# Arkham's entity database instead of relying only on your manual list.
# Get a key at https://intel.arkm.com/api — free tier availability and
# rate limits can change, so check current terms before relying on it.
ARKHAM_ENABLED = False
ARKHAM_API_KEY = os.environ.get("ARKHAM_API_KEY", "")
ARKHAM_ADDRESS_URL = "https://api.arkm.com/intelligence/address/{address}"
ARKHAM_CHECK_TOP_N_HOLDERS = 5      # keep low — this API is rate-limited

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"


# ---------------------------------------------------------------------
# DATA FETCHING
# ---------------------------------------------------------------------

def get_candidate_pairs():
    """
    Pull Solana pairs from DexScreener's search endpoint.
    We search a broad term and filter client-side to Solana chain,
    since DexScreener doesn't offer a pure "brand new pairs" feed on
    the free tier. Swap this for Birdeye's /defi/v2/tokens/new_listing
    endpoint if you have an API key — it's more precise.
    """
    try:
        resp = requests.get(DEXSCREENER_SEARCH_URL, params={"q": "solana"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", []) or []
        return [p for p in pairs if p.get("chainId") == "solana"]
    except requests.RequestException as e:
        print(f"[dexscreener] fetch failed: {e}")
        return []


def get_rugcheck_report(mint_address):
    """Fetch RugCheck's risk report for a given token mint address."""
    try:
        resp = requests.get(RUGCHECK_REPORT_URL.format(mint=mint_address), timeout=10)
        if resp.status_code != 200:
            return None
        return resp.json()
    except requests.RequestException as e:
        print(f"[rugcheck] fetch failed for {mint_address}: {e}")
        return None


# ---------------------------------------------------------------------
# SOCIAL FILTER
# ---------------------------------------------------------------------

def get_x_link(pair):
    """
    Returns the X/Twitter URL if DexScreener has one attached to this
    pair's token info, else None. DexScreener pulls this from the
    token's on-chain metadata / DEX listing form, so an absent link
    usually means the team never filled it in (or it's a raw contract
    deploy with no project behind it yet).
    """
    info = pair.get("info", {}) or {}
    for social in info.get("socials", []) or []:
        stype = (social.get("type") or "").lower()
        url = social.get("url", "")
        if stype == "twitter" or "x.com" in url or "twitter.com" in url:
            return url
    return None


# ---------------------------------------------------------------------
# KOL / SMART MONEY DETECTION
# ---------------------------------------------------------------------

def get_arkham_label(address):
    """
    Looks up a Solana address in Arkham's entity/label database.
    Returns a display string like "Some Fund (fund)" if Arkham has a
    label for it, else None. Requires ARKHAM_API_KEY.

    NOTE: I couldn't test this against the live API while building it
    (no network access in my sandbox). The response shape below
    (arkhamEntity / arkhamLabel) matches Arkham's documented schema as
    of writing, but confirm with a raw print(resp.json()) the first
    time you run this in case field names have shifted.
    """
    if not ARKHAM_ENABLED or not ARKHAM_API_KEY:
        return None
    try:
        resp = requests.get(
            ARKHAM_ADDRESS_URL.format(address=address),
            headers={"API-Key": ARKHAM_API_KEY},
            params={"chain": "solana"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()

        entity = data.get("arkhamEntity") or {}
        label = data.get("arkhamLabel") or {}

        name = entity.get("name") or label.get("name")
        etype = entity.get("type")
        if not name:
            return None
        return f"{name} ({etype})" if etype else name
    except requests.RequestException as e:
        print(f"[arkham] lookup failed for {address}: {e}")
        return None


def check_kol_involvement(rug_report):
    """
    Checks the token's creator wallet and top holders against your
    KNOWN_KOL_WALLETS watchlist, and (if enabled) against Arkham's
    entity database for auto-labeling. Returns a list of
    (address, name) matches — empty if none found.
    """
    if not rug_report:
        return []

    matches = []
    creator = (rug_report.get("creator") or "").strip()
    checked = set()

    def check_address(addr, extra_label=""):
        if not addr or addr in checked:
            return
        checked.add(addr)

        if addr in KNOWN_KOL_WALLETS:
            matches.append((addr, KNOWN_KOL_WALLETS[addr] + extra_label))
            return  # manual list takes priority, skip the Arkham call

        arkham_name = get_arkham_label(addr)
        if arkham_name:
            matches.append((addr, arkham_name + extra_label))

    check_address(creator, " (creator/dev)")

    top_holders = rug_report.get("topHolders", [])
    for h in top_holders[:ARKHAM_CHECK_TOP_N_HOLDERS]:
        addr = (h.get("address") or "").strip()
        if addr != creator:
            check_address(addr, f" ({h.get('pct', 0):.1f}% held)")

    return matches


# ---------------------------------------------------------------------
# SCORING
# ---------------------------------------------------------------------

def score_token(pair, rug_report, kol_matches=None):
    """
    Returns (score 0-100, list_of_reasons).
    Higher score = looks healthier. This is intentionally transparent
    so you can see exactly why a token passed or failed.
    """
    score = 100
    reasons = []

    if kol_matches:
        score += KOL_SCORE_BOOST
        names = ", ".join(name for _, name in kol_matches)
        reasons.insert(0, f"🔥 KOL involvement: {names}")

    liquidity_usd = float(pair.get("liquidity", {}).get("usd") or 0)
    if liquidity_usd < MIN_LIQUIDITY_USD:
        score -= 25
        reasons.append(f"Low liquidity: ${liquidity_usd:,.0f}")

    # Market cap can come back as marketCap or fdv depending on the pair;
    # fall back to fdv (fully diluted valuation) if marketCap isn't set.
    market_cap_usd = float(pair.get("marketCap") or pair.get("fdv") or 0)
    if market_cap_usd < MIN_MARKET_CAP_USD:
        score -= 15
        reasons.append(f"Low market cap: ${market_cap_usd:,.0f}")

    volume_24h_usd = float(pair.get("volume", {}).get("h24") or 0)
    if volume_24h_usd < MIN_VOLUME_24H_USD:
        score -= 15
        reasons.append(f"Low 24h volume: ${volume_24h_usd:,.0f}")

    if not rug_report:
        score -= 30
        reasons.append("No RugCheck report available (unverified token)")
        return max(min(score, 100), 0), reasons

    # Liquidity lock / burn status
    lp_locked_pct = rug_report.get("totalLPLockedPct", 0)
    if lp_locked_pct < MIN_LIQUIDITY_LOCKED_PCT:
        score -= 25
        reasons.append(f"Only {lp_locked_pct}% of LP locked/burned")

    # Holder count — RugCheck reports this as totalHolders in most responses;
    # verify this field name against a live response if it reads as 0 for
    # tokens you know have plenty of holders.
    holder_count = rug_report.get("totalHolders", 0)
    if holder_count < MIN_HOLDER_COUNT:
        score -= 15
        reasons.append(f"Only {holder_count} holders (min {MIN_HOLDER_COUNT})")

    # Mint & freeze authority
    if REQUIRE_MINT_AUTHORITY_REVOKED and not rug_report.get("mintAuthorityRevoked", False):
        score -= 15
        reasons.append("Mint authority NOT revoked (dev can print more supply)")

    if REQUIRE_FREEZE_AUTHORITY_REVOKED and not rug_report.get("freezeAuthorityRevoked", False):
        score -= 10
        reasons.append("Freeze authority NOT revoked (dev can freeze your wallet)")

    # Holder concentration
    top_holders = rug_report.get("topHolders", [])
    top10_pct = sum(h.get("pct", 0) for h in top_holders[:10])
    if top10_pct > MAX_TOP10_HOLDER_PCT:
        score -= 20
        reasons.append(f"Top 10 holders own {top10_pct:.1f}% of supply")

    for h in top_holders:
        if h.get("pct", 0) > MAX_SINGLE_HOLDER_PCT and not h.get("isLP", False):
            score -= 10
            reasons.append(f"Single wallet holds {h['pct']:.1f}% ({h.get('address','')[:6]}...)")
            break

    # Dev/creator wallet holding — checked specifically, separate from the
    # generic single-wallet rule above, since a dev holding a lot is a
    # different (often worse) signal than a random early buyer holding a lot.
    creator = (rug_report.get("creator") or "").strip()
    dev_pct = 0
    for h in top_holders:
        if (h.get("address") or "").strip() == creator:
            dev_pct = h.get("pct", 0)
            break
    if dev_pct > MAX_DEV_HOLDING_PCT:
        score -= 20
        reasons.append(f"Dev/creator wallet holds {dev_pct:.1f}% of supply (limit {MAX_DEV_HOLDING_PCT}%)")

    # Bundled / insider / sniper clusters — RugCheck flags these as "risks"
    for risk in rug_report.get("risks", []):
        name = risk.get("name", "").lower()
        if any(k in name for k in ["bundle", "insider", "sniper", "cluster"]):
            score -= 20
            reasons.append(f"Flagged risk: {risk.get('description', risk.get('name'))}")

    return max(min(score, 100), 0), reasons


# ---------------------------------------------------------------------
# TELEGRAM ALERTS
# ---------------------------------------------------------------------

def send_telegram_alert(pair, score, reasons, x_link=None, kol_matches=None):
    base_token = pair.get("baseToken", {})
    name = base_token.get("name", "Unknown")
    symbol = base_token.get("symbol", "?")
    mint = base_token.get("address", "")
    url = pair.get("url", f"https://dexscreener.com/solana/{mint}")
    liquidity = float(pair.get("liquidity", {}).get("usd") or 0)

    prefix = "🔥 KOL PRIORITY 🔥\n" if kol_matches else ""
    verdict = "✅ Looks relatively healthy" if score >= MIN_SCORE_TO_ALERT else "⚠️ Mixed signals"
    reason_lines = "\n".join(f"• {r}" for r in reasons) if reasons else "• No major red flags found"
    social_line = f"X: {x_link}\n" if x_link else ""

    message = (
        f"{prefix}"
        f"*{name} (${symbol})*\n"
        f"Score: *{score}/100* — {verdict}\n"
        f"Liquidity: ${liquidity:,.0f}\n"
        f"{social_line}\n"
        f"{reason_lines}\n\n"
        f"[View on DexScreener]({url})\n\n"
        f"_Screening heuristic only — not financial advice. Verify yourself before entering._"
    )

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[telegram] send failed: {e}")


# ---------------------------------------------------------------------
# STATE / DEDUP
# ---------------------------------------------------------------------

def load_seen_tokens():
    if os.path.exists(SEEN_TOKENS_FILE):
        with open(SEEN_TOKENS_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_tokens(seen):
    with open(SEEN_TOKENS_FILE, "w") as f:
        json.dump(list(seen), f)


# ---------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------

def print_active_config():
    print("=" * 60)
    print("ACTIVE SCREENING CONFIG")
    print("=" * 60)
    print(f"  Min liquidity:          ${MIN_LIQUIDITY_USD:,}")
    print(f"  Min LP locked/burned:   {MIN_LIQUIDITY_LOCKED_PCT}%")
    print(f"  Min market cap:         ${MIN_MARKET_CAP_USD:,}")
    print(f"  Min 24h volume:         ${MIN_VOLUME_24H_USD:,}")
    print(f"  Min holder count:       {MIN_HOLDER_COUNT}")
    print(f"  Max top 10 holders:     {MAX_TOP10_HOLDER_PCT}%")
    print(f"  Max single holder:      {MAX_SINGLE_HOLDER_PCT}%")
    print(f"  Max dev/creator hold:   {MAX_DEV_HOLDING_PCT}%")
    print(f"  Require mint revoked:   {REQUIRE_MINT_AUTHORITY_REVOKED}")
    print(f"  Require freeze revoked: {REQUIRE_FREEZE_AUTHORITY_REVOKED}")
    print(f"  Require X/Twitter:      {REQUIRE_X_SOCIAL}")
    print(f"  Min score to alert:     {MIN_SCORE_TO_ALERT}/100")
    print(f"  KOL wallets tracked:    {len(KNOWN_KOL_WALLETS)}")
    print(f"  Arkham auto-labeling:   {ARKHAM_ENABLED}")
    print(f"  Poll interval:          {POLL_INTERVAL_SECONDS}s")
    print("=" * 60)


def main():
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        print("!! Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before running (env vars or edit the file).")
        return

    print_active_config()

    seen = load_seen_tokens()
    print(f"Starting screener. Already tracking {len(seen)} tokens. Polling every {POLL_INTERVAL_SECONDS}s.")

    while True:
        pairs = get_candidate_pairs()
        new_count = 0

        for pair in pairs:
            mint = pair.get("baseToken", {}).get("address")
            if not mint or mint in seen:
                continue

            seen.add(mint)
            new_count += 1

            x_link = get_x_link(pair)
            if REQUIRE_X_SOCIAL and not x_link:
                symbol = pair.get("baseToken", {}).get("symbol", "?")
                print(f"{symbol:>10} | skipped (no X/Twitter linked) | {mint}")
                continue

            rug_report = get_rugcheck_report(mint)
            kol_matches = check_kol_involvement(rug_report)
            score, reasons = score_token(pair, rug_report, kol_matches)

            flag = " 🔥KOL" if kol_matches else ""
            print(f"{pair.get('baseToken', {}).get('symbol', '?'):>10} | score={score:3d}{flag} | {mint}")

            if score >= MIN_SCORE_TO_ALERT or (kol_matches and KOL_ALWAYS_ALERTS):
                send_telegram_alert(pair, score, reasons, x_link, kol_matches)

            time.sleep(1)  # be polite to RugCheck's rate limit

        if new_count:
            save_seen_tokens(seen)

        if RUN_ONCE:
            print("RUN_ONCE mode — single pass complete, exiting.")
            break

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
