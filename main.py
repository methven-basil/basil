# -*- coding: utf-8 -*-
import os
import json
import re
import random
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from functools import wraps

from flask import (Flask, request, Response,
                   render_template_string, redirect, session)
from twilio.rest import Client as TwilioClient
import anthropic
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'basil-fox-secret-changeme')

# ── Config ────────────────────────────────────────────────────────────────────

TWILIO_ACCOUNT_SID = os.environ['TWILIO_ACCOUNT_SID']
TWILIO_AUTH_TOKEN  = os.environ['TWILIO_AUTH_TOKEN']
TWILIO_FROM        = 'whatsapp:+14155238886'
ANTHROPIC_API_KEY  = os.environ['ANTHROPIC_API_KEY']
SUPABASE_URL       = os.environ['SUPABASE_URL']
SUPABASE_KEY       = os.environ['SUPABASE_KEY']
ADMIN_PASSWORD     = os.environ['ADMIN_PASSWORD']
DAILY_QUERY_LIMIT  = int(os.environ.get('DAILY_QUERY_LIMIT', '20'))
RAILWAY_HOST       = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '')

# ── Affiliate IDs ─────────────────────────────────────────────────────────────
AFFILIATE_IDS = {
    'paddypower':  os.environ.get('AFFILIATE_PADDYPOWER', ''),
    'bet365':      os.environ.get('AFFILIATE_BET365', ''),
    'williamhill': os.environ.get('AFFILIATE_WILLIAMHILL', ''),
}

# ── Clients ───────────────────────────────────────────────────────────────────

twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
db     = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Database helpers ──────────────────────────────────────────────────────────

def get_user(phone):
    r = db.table('users').select('*').eq('phone_number', phone).execute()
    return r.data[0] if r.data else None

def register_user(phone, code):
    db.table('users').insert({
        'phone_number':  phone,
        'invite_code':   code,
        'registered_at': datetime.utcnow().isoformat(),
        'blocked':       False
    }).execute()

def get_invite_code(code):
    r = db.table('invite_codes').select('*').eq('code', code).execute()
    return r.data[0] if r.data else None

def increment_invite_code(code):
    row = db.table('invite_codes').select('uses_count').eq('code', code).execute().data[0]
    db.table('invite_codes').update(
        {'uses_count': row['uses_count'] + 1}
    ).eq('code', code).execute()

def log_query(phone, query, response):
    db.table('queries').insert({
        'phone_number': phone,
        'query':        query,
        'response':     response,
        'queried_at':   datetime.utcnow().isoformat()
    }).execute()

def get_today_count(phone):
    today = date.today().isoformat()
    r = db.table('queries').select('id')\
        .eq('phone_number', phone)\
        .gte('queried_at', today)\
        .not_.like('response', 'BLOCKED%')\
        .execute()
    return len(r.data)

# ── Cache helpers ─────────────────────────────────────────────────────────────

def cache_key(query):
    return f"{date.today().isoformat()}:{query.lower().strip()}"

def get_cache(query):
    try:
        r = db.table('cache').select('data').eq('cache_key', cache_key(query)).execute()
        if r.data:
            print(f"Cache hit: {query}")
            return json.loads(r.data[0]['data'])
    except Exception as e:
        print(f"Cache read error: {e}")
    return None

def set_cache(query, data):
    try:
        db.table('cache').upsert({
            'cache_key': cache_key(query),
            'data':      json.dumps(data),
            'cached_at': datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"Cache write error: {e}")

# ── Pending disambiguation helpers ────────────────────────────────────────────

def get_pending(phone):
    try:
        r = db.table('pending').select('*').eq('phone_number', phone).execute()
        return r.data[0] if r.data else None
    except Exception:
        return None

def set_pending(phone, options, original_query):
    try:
        db.table('pending').upsert({
            'phone_number':   phone,
            'options':        json.dumps(options),
            'original_query': original_query,
            'created_at':     datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"Pending write error: {e}")

def clear_pending(phone):
    try:
        db.table('pending').delete().eq('phone_number', phone).execute()
    except Exception as e:
        print(f"Pending clear error: {e}")

# ── Gatekeeper ────────────────────────────────────────────────────────────────

GATEKEEPER_PROMPT = """\
You are a one-word classifier for a UK sports TV listings bot.

The user sent: "{message}"

Sports teams often have short or place-based names like "Bath", "Hull", "Sale", "Wasps",
"Saints", "Blues", "Reds", "City", "United", "Rangers", "Celtic", "Ajax", "Lyon" etc.
Be generous - if there is any reasonable chance this could be a sports team name, say "sport".

Reply with ONLY one of these three words - nothing else:

sport     - if this could plausibly be a sports team name or nickname, or the words football / rugby / soccer
unclear   - if it is genuinely ambiguous and could not be a team (e.g. a partial sentence, random letters)
offtopic  - if this is clearly not sports-related (a question, general chat, gibberish, instructions)"""

def check_intent(message):
    try:
        r = claude.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=5,
            messages=[{'role': 'user', 'content': GATEKEEPER_PROMPT.format(message=message)}]
        )
        verdict = (r.content[0].text or '').strip().lower().split()[0]
        print(f"Gatekeeper: '{message}' → {verdict}")
        return verdict if verdict in ('sport', 'unclear', 'offtopic') else 'sport'
    except Exception as e:
        print(f"Gatekeeper error: {e}")
        return 'sport'

# ── Claude prompts ────────────────────────────────────────────────────────────

TEAM_PROMPT = """\
You are Basil - a sharp, witty fox who helps UK sports fans find their team on TV.

Today is {today}.

The user has sent: "{query}"

Your tasks:
1. Work out if this is a football or rugby union team (use common sense).
2. Search wheresthematch.com to find whether they are playing TODAY or TOMORROW.
3. If playing today or tomorrow: get the UK TV channel, kick-off time, coverage start time, and current odds \
from a UK bookmaker (Paddy Power, Bet365 or William Hill).
4. If NOT playing today or tomorrow: find their very next fixture - date, TV channel, kick-off time.
5. For both cases: check if the match is also on UK radio (BBC Radio 5 Live, talkSPORT, \
BBC Radio Scotland, BBC Radio Wales, BBC Radio Ulster). Only include radio if you are confident \
it is being broadcast. Leave radio_station blank if unsure - do not guess.
6. Write a fox fact: genuinely surprising, based on real fox behaviour, under 60 words, \
ends with a one-liner connecting fox instinct to having a wager on this match.

IMPORTANT - always report from the perspective of the team the user searched for.
Always show the searched team first, then their opponent. Make clear in the venue field \
whether the searched team is home or away (e.g. "Away at Stade Atlantique, Bordeaux" if away).

IMPORTANT - if the match is TODAY, set playing_today to true.
If the match is TOMORROW or within 24 hours, still set playing_today to false but set \
next_date to "Tomorrow" rather than the full date.

IMPORTANT - if multiple very different teams could match this name (e.g. "Saints" = Southampton FC,
Northampton Saints rugby, St Mirren FC), return the ambiguous response instead.
Only use ambiguous if teams are from genuinely different sports or leagues - don't overthink it.

Return ONLY valid JSON - no markdown, no explanation, no backticks.

If playing TODAY:
{{"playing_today":true,"sport":"rugby","home_team":"","away_team":"","competition":"","venue":"","kickoff":"","coverage_start":"","tv_channel":"","radio_station":"","home_odds":"","draw_odds":"","away_odds":"","bookmaker":"","bookmaker_url":"","fox_fact":""}}

If NOT playing today:
{{"playing_today":false,"sport":"rugby","home_team":"","away_team":"","competition":"","venue":"","next_date":"","kickoff":"","tv_channel":"","radio_station":"","home_odds":"","draw_odds":"","away_odds":"","bookmaker":"","bookmaker_url":"","fox_fact":""}}

If AMBIGUOUS (multiple plausible matches from different sports/leagues):
{{"ambiguous":true,"options":[{{"label":"Full Team Name (sport)","query":"Exact search term"}}]}}

If team cannot be identified at all:
{{"clarify":true,"message":"Short friendly message asking user to check spelling."}}"""

SPORT_PROMPT = """\
You are Basil - a sharp, witty fox who helps UK sports fans find what's on TV.

Today is {today}.

The user wants to know what {sport} matches are on UK TV TODAY.

Search wheresthematch.com for today's {sport} TV listings. Pick the 4-5 most notable matches.

Write a fox fact under 50 words that ends with a witty nudge toward having a flutter.

Return ONLY valid JSON - no markdown, no explanation, no backticks:

{{"sport":"{sport}","matches":[{{"home_team":"","away_team":"","competition":"","kickoff":"","tv_channel":""}}],"fox_fact":"","bookmaker":"Paddy Power","bookmaker_url":"https://www.paddypower.com"}}"""

GAMBLING_REMINDER = "\n\n_18+ Gamble responsibly. begambleaware.org_"

STRICT_SUFFIX = """

CRITICAL: Return ONLY the JSON object. Not a single word before or after.
If uncertain, use "Unknown" rather than adding narrative."""

# ── Competitions to pre-fetch each morning ────────────────────────────────────

PREFETCH_COMPETITIONS = [
    ('English Premier League',          'football'),
    ('Scottish Premiership',            'football'),
    ('Scottish FA Cup',                 'football'),
    ('UEFA Champions League',           'football'),
    ('FIFA World Cup',                  'football'),
    ('Gallagher Premiership',           'rugby'),
    ('United Rugby Championship',       'rugby'),
    ('Investec Champions Cup',          'rugby'),
    ('European Rugby Challenge Cup',    'rugby'),
    ('Six Nations',                     'rugby'),
    ('Super Rugby',                     'rugby'),
    ('Rugby World Cup',                 'rugby'),
]

COMPETITION_PREFETCH_PROMPT = """\
You are Basil. Today is {today}.

Search wheresthematch.com for any {competition} matches broadcast on UK TV today.
For each match, also get current UK betting odds (Paddy Power, Bet365 or William Hill).
Write a short fox fact for each match - real fox behaviour, under 50 words, \
ends with a nudge to have a bet on this specific match.

If there are NO matches today: {{"competition":"{competition}","matches":[]}}

If matches found:
{{"competition":"{competition}","matches":[{{"home_team":"","away_team":"","sport":"{sport}","kickoff":"","coverage_start":"","tv_channel":"","venue":"","home_odds":"","draw_odds":"","away_odds":"","bookmaker":"","bookmaker_url":"","fox_fact":""}}]}}

Return ONLY valid JSON."""

# ── Claude caller ─────────────────────────────────────────────────────────────

def extract_json(text):
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find('{')
    end   = text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except Exception:
            pass
    return None

def call_claude(prompt):
    for attempt in range(2):
        p = prompt if attempt == 0 else prompt + STRICT_SUFFIX

        # Try up to 3 times on rate limit (30s apart)
        for retry in range(3):
            try:
                try:
                    response = claude.messages.create(
                        model='claude-sonnet-4-6',
                        max_tokens=2000,
                        messages=[{'role': 'user', 'content': p}],
                        tools=[{'type': 'web_search_20250305', 'name': 'web_search'}]
                    )
                except Exception as e:
                    if 'web_search' in str(e).lower() or 'tool' in str(e).lower():
                        print(f"Tool call failed, retrying without tools")
                        response = claude.messages.create(
                            model='claude-sonnet-4-6',
                            max_tokens=2000,
                            messages=[{'role': 'user', 'content': p}]
                        )
                    else:
                        raise
                break  # success - exit retry loop

            except Exception as e:
                if '429' in str(e) or 'rate_limit' in str(e).lower():
                    wait = 30 * (retry + 1)
                    print(f"Rate limited. Waiting {wait}s (retry {retry+1}/3)")
                    time.sleep(wait)
                    if retry == 2:
                        raise
                else:
                    raise

        text = ''.join((getattr(b, 'text', '') or '')
                       for b in response.content if getattr(b, 'type', '') == 'text')
        text = re.sub(r'^```(?:json)?\s*', '', text.strip())
        text = re.sub(r'\s*```$', '', text).strip()
        print(f"Attempt {attempt+1}: '{text[:200]}'")

        parsed = extract_json(text)
        if parsed and isinstance(parsed, dict):
            return parsed
        print(f"Attempt {attempt+1}: no valid JSON, retrying")

    raise ValueError("Could not extract valid JSON after 2 attempts")

def basil_team(query):
    cached = get_cache(query)
    if cached:
        return cached
    today  = datetime.now().strftime('%A %d %B %Y')
    result = call_claude(TEAM_PROMPT.format(today=today, query=query))
    if result and not result.get('ambiguous') and not result.get('clarify'):
        set_cache(query, result)
    return result

def basil_sport(sport):
    cached = get_cache(sport)
    if cached:
        return cached
    today  = datetime.now().strftime('%A %d %B %Y')
    result = call_claude(SPORT_PROMPT.format(today=today, sport=sport))
    if result:
        set_cache(sport, result)
    return result

# ── Bookmaker redirect (prevents WhatsApp link preview + tracks clicks) ───────

BK_MAP = {
    'pp':   ('https://www.paddypower.com',  'paddypower'),
    'b365': ('https://www.bet365.com',      'bet365'),
    'wh':   ('https://www.williamhill.com', 'williamhill'),
}

@app.route('/go/<bk>')
def go(bk):
    """Redirect to bookmaker. Short URL prevents WhatsApp generating a link preview."""
    base, key = BK_MAP.get(bk, ('https://www.paddypower.com', 'paddypower'))
    aff = AFFILIATE_IDS.get(key, '')
    sep = '&' if '?' in base else '?'
    url = f"{base}{sep}af={aff}" if aff else base
    return redirect(url, 302)

def make_bet_url(base_url, bookmaker):
    """Return a short /go/ URL - no OG tags so WhatsApp will not generate a preview."""
    shortcodes = {
        'paddypower': 'pp', 'paddy power': 'pp',
        'bet365': 'b365',
        'williamhill': 'wh', 'william hill': 'wh',
    }
    sc = shortcodes.get(bookmaker.lower().strip(), '')
    if sc and RAILWAY_HOST:
        return f"https://{RAILWAY_HOST}/go/{sc}"
    # Fallback: strip scheme so WhatsApp won't auto-preview
    return (base_url or '').replace('https://', '').replace('http://', '')

# ── Message formatters ────────────────────────────────────────────────────────

def sport_emoji(sport):
    return '🏉' if str(sport).lower() == 'rugby' else '⚽'

LEICESTER_FACTS = [
    "In 2016, Leicester City won the Premier League at 5000/1. The biggest upset in football history. Orchestrated by foxes. Of course it was. I have never been more proud of anything in my life. Back them. Always back them. We're foxes.",
    "In 2016, Leicester City won the Premier League at 5000/1. Every pundit said it was impossible. Every model said it was impossible. The foxes didn't get the memo. We never do.",
    "In 2016, Leicester City won the Premier League at 5000/1. Ranieri celebrated with pizza. The players celebrated with each other. The foxes of England celebrated with something primal and ancient. We knew all along.",
    "In 2016, Leicester City won the Premier League at 5000/1. Bookmakers lost millions. Dreamers won everything. It remains the greatest thing a fox has ever done. And foxes have done extraordinary things.",
    "In 2016, Leicester City won the Premier League at 5000/1. I was there in spirit. Every fox was. It is the one result in football history that required no explanation if you understand how foxes think.",
]

LEICESTER_TRIGGERS = {'leicester', 'leicester city', 'the foxes', 'lcfc', 'leicester fc'}

def is_leicester(body):
    return body.lower().strip() in LEICESTER_TRIGGERS

def fmt_leicester(d):
    """Special Easter egg formatter for Leicester City."""
    home = d.get('home_team', 'Leicester City')
    away = d.get('away_team', '?')
    fact = random.choice(LEICESTER_FACTS)

    lines = [
        "🦊 *Wait.*\n",
        f"*Leicester City. The Foxes.*\n",
        "*One of us.*\n",
    ]

    if d.get('playing_today'):
        lines += [
            f"⚽ *{home} vs {away}*",
            f"{d.get('competition','')} | {d.get('venue','')}".strip(' |') + '\n',
            f"📺 *{d.get('tv_channel','TBC')}*",
        ]
        cov = d.get('coverage_start')
        lines.append(f"Coverage {cov} | KO {d.get('kickoff','TBC')}" if cov else f"KO {d.get('kickoff','TBC')}")
        if d.get('radio_station'):
            lines.append(f"📻 Also on {d['radio_station']}")
        if d.get('home_odds') and d.get('home_odds') not in ('', 'Unknown'):
            lines += [
                '',
                f"💰 *Odds ({d.get('bookmaker','Paddy Power')})*",
                f"{home}: {d.get('home_odds','')} | Draw: {d.get('draw_odds','')} | {away}: {d.get('away_odds','')}\n",
            ]
    else:
        next_date = d.get('next_date', 'TBC')
        when = "Tomorrow" if next_date == "Tomorrow" else next_date
        lines += [
            f"⚽ *{home} vs {away}*",
            f"{d.get('competition','')} | {d.get('venue','')}".strip(' |'),
        ]
        tv = d.get('tv_channel', '')
        radio = d.get('radio_station', '')
        if tv and tv.lower() not in ('', 'unknown', 'none'):
            lines.append(f"📺 {tv} — {when}, KO {d.get('kickoff','TBC')}")
        else:
            lines.append(f"📺 Not yet confirmed — {when}, KO {d.get('kickoff','TBC')}")
        if radio:
            lines.append(f"📻 Also on {radio}")
        if d.get('home_odds') and d.get('home_odds') not in ('', 'Unknown'):
            lines += [
                '',
                f"💰 *Early odds ({d.get('bookmaker','Paddy Power')})*",
                f"{home}: {d.get('home_odds','')} | Draw: {d.get('draw_odds','')} | {away}: {d.get('away_odds','')}\n",
            ]

    lines += [
        "\n🦊 *Basil is beside himself:*",
        fact,
    ]
    return '\n'.join(lines)

def fmt_team(d, body=''):
    e   = sport_emoji(d.get('sport', ''))
    url = make_bet_url(d.get('bookmaker_url', ''), d.get('bookmaker', 'Paddy Power'))

    if d.get('playing_today'):
        lines = [
            "🦊 *Basil's tip for today...*\n",
            f"{e} *{d.get('home_team','?')} vs {d.get('away_team','?')}*",
            f"{d.get('competition','')} | {d.get('venue','')}".strip(' |') + '\n',
            f"📺 *{d.get('tv_channel','TBC')}*",
        ]
        cov = d.get('coverage_start')
        lines.append(f"Coverage {cov} | KO {d.get('kickoff','TBC')}" if cov else f"KO {d.get('kickoff','TBC')}")
        if d.get('radio_station'):
            lines.append(f"📻 Also on {d['radio_station']}")
        if d.get('home_odds') and d.get('home_odds') not in ('', 'Unknown'):
            lines += [
                '',
                f"💰 *Odds ({d.get('bookmaker','Paddy Power')})*",
                f"{d.get('home_team','?')}: {d.get('home_odds','')} | Draw: {d.get('draw_odds','')} | {d.get('away_team','?')}: {d.get('away_odds','')}\n",
            ]
        lines += [
            "🦊 *Basil says:*",
            d.get('fox_fact', ''),
        ]
    else:
        searched = d.get('home_team', body)
        next_date = d.get('next_date', 'TBC')
        when = "Tomorrow" if next_date == "Tomorrow" else next_date
        lines = [
            f"🦊 *{searched} are playing {when}.*\n" if next_date == "Tomorrow"
            else f"🦊 *{searched} aren't on TV today.*\n",
            f"*{d.get('home_team','?')} vs {d.get('away_team','TBC')}*",
            f"{d.get('competition','')} | {d.get('venue','')}",
        ]
        tv = d.get('tv_channel', '')
        radio = d.get('radio_station', '')
        if tv and tv.lower() not in ('', 'unknown', 'none'):
            lines.append(f"📺 {tv} - {d.get('next_date','TBC')}, KO {d.get('kickoff','TBC')}")
        elif radio:
            lines.append(f"📻 {radio} - {d.get('next_date','TBC')}, KO {d.get('kickoff','TBC')}")
        else:
            lines.append(f"📺 Not yet confirmed - {d.get('next_date','TBC')}, KO {d.get('kickoff','TBC')}")
        if tv and radio:
            lines.append(f"📻 Also on {radio}")
        lines += [
            '',
            "🦊 *Basil says:*",
            d.get('fox_fact', ''),
        ]
        if d.get('home_odds') and d.get('home_odds') not in ('', 'Unknown'):
            lines += [
                '',
                f"💰 *Early odds ({d.get('bookmaker', 'Paddy Power')})*",
                f"{d.get('home_team','?')}: {d['home_odds']} | Draw: {d.get('draw_odds','')} | {d.get('away_team','?')}: {d.get('away_odds','')}\n",
            ]
    return '\n'.join(lines)

def fmt_sport(d):
    e   = sport_emoji(d.get('sport', ''))
    url = make_bet_url(d.get('bookmaker_url', ''), d.get('bookmaker', 'Paddy Power'))
    lines = ["🦊 *Basil's picks for today...*\n"]
    for m in d.get('matches', []):
        lines.append(f"{e} *{m['home_team']} vs {m['away_team']}*")
        lines.append(f"{m['competition']} - {m['tv_channel']}, KO {m['kickoff']}\n")
    lines += ["🦊 *Basil says:*", d.get('fox_fact', '')]
    return '\n'.join(lines)

def fmt_ambiguous(options, original):
    lines = [f"🦊 A few teams go by *{original}* - which did you mean?\n"]
    for i, opt in enumerate(options, 1):
        lines.append(f"{i}️⃣ {opt['label']}")
    lines.append("\nReply with the number.")
    return '\n'.join(lines)

# ── Async query processor ─────────────────────────────────────────────────────

def send(to, body):
    twilio.messages.create(from_=TWILIO_FROM, to=to, body=body)

def process_async(from_wa, body, phone):
    """Runs in background thread. Does the Claude work then sends reply."""
    try:
        q = body.lower().strip()
        if q in ['football', 'soccer']:
            data  = basil_sport('football')
            reply = fmt_sport(data)
        elif q == 'rugby':
            data  = basil_sport('rugby')
            reply = fmt_sport(data)
        else:
            data = basil_team(body)
            if data.get('ambiguous'):
                options = data.get('options', [])
                set_pending(phone, options, body)
                reply = fmt_ambiguous(options, body)
            elif data.get('clarify'):
                reply = f"🦊 {data.get('message', 'Not sure who you mean - could you check the team name?')}"
            elif is_leicester(body):
                reply = fmt_leicester(data)
            else:
                reply = fmt_team(data, body)

        log_query(phone, body, reply)
        send(from_wa, reply)

    except Exception as ex:
        msg = str(ex)
        print(f'ERROR processing "{body}": {msg}')
        if '429' in msg or 'rate_limit' in msg.lower():
            send(from_wa, "🦊 Basil's getting a lot of requests right now - give it a minute and try again.")
        else:
            send(from_wa, "🦊 Basil's nose is twitching - something went wrong. Try again in a moment.")

# ── Webhook ───────────────────────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    from_wa = request.form.get('From', '')
    body    = request.form.get('Body', '').strip()
    phone   = from_wa.replace('whatsapp:', '')
    user    = get_user(phone)

    # ── JOIN ──────────────────────────────────────────────────────────────────
    if body.upper().startswith('JOIN'):
        parts = body.split(None, 1)
        code  = parts[1].upper().strip() if len(parts) > 1 else ''
        if user:
            send(from_wa, "🦊 You're already registered! Just send me a team name.")
        elif not code:
            send(from_wa, "🦊 Send *JOIN yourcode* to get started.")
        else:
            invite = get_invite_code(code)
            if not invite:
                send(from_wa, "🦊 That code doesn't look right. Double-check with whoever invited you.")
            elif invite['uses_count'] >= invite['max_uses']:
                send(from_wa, "🦊 That invite code has run out. Ask for a fresh one.")
            else:
                register_user(phone, code)
                increment_invite_code(code)
                send(from_wa,
                     "🦊 *Welcome to Basil!*\n\n"
                     "Send me any team name and I'll tell you what channel they're on, "
                     "the latest odds, and a little something to think about.\n\n"
                     "Go on then - try a team name.")
        return Response('', 204)

    # ── Not registered ────────────────────────────────────────────────────────
    if not user:
        send(from_wa, "🦊 You'll need an invite code to use Basil.\nSend *JOIN yourcode* to get started.")
        return Response('', 204)

    # ── Blocked ───────────────────────────────────────────────────────────────
    if user.get('blocked'):
        return Response('', 204)

    # ── Resolve pending disambiguation ────────────────────────────────────────
    if body.strip() in ['1', '2', '3', '4', '5']:
        pending = get_pending(phone)
        if pending:
            options = json.loads(pending.get('options', '[]'))
            idx = int(body.strip()) - 1
            if 0 <= idx < len(options):
                clear_pending(phone)
                body = options[idx]['query']
                send(from_wa, "🦊 *On it...*")
                t = threading.Thread(target=process_async, args=(from_wa, body, phone))
                t.daemon = True
                t.start()
                return Response('', 204)

    # ── Rate limit ────────────────────────────────────────────────────────────
    if get_today_count(phone) >= DAILY_QUERY_LIMIT:
        send(from_wa, f"🦊 You've had {DAILY_QUERY_LIMIT} queries today - even foxes need a rest. Try again tomorrow.")
        return Response('', 204)

    # ── Gatekeeper ────────────────────────────────────────────────────────────
    q = body.lower().strip()
    if q not in ['football', 'rugby', 'soccer']:
        intent = check_intent(body)
        if intent == 'offtopic':
            log_query(phone, body, 'BLOCKED:offtopic')
            send(from_wa, "🦊 Basil's a sports fox, not a general assistant. Send me a team name and I'll tell you what channel they're on.")
            return Response('', 204)
        if intent == 'unclear':
            log_query(phone, body, 'BLOCKED:unclear')
            send(from_wa, f"🦊 Not sure who you mean by *{body}* - could you check the spelling or give me the full team name?")
            return Response('', 204)

    # ── Fire and forget ───────────────────────────────────────────────────────
    send(from_wa, "🦊 *On it...*")
    t = threading.Thread(target=process_async, args=(from_wa, body, phone))
    t.daemon = True
    t.start()
    return Response('', 204)

# ── 9am pre-fetch ─────────────────────────────────────────────────────────────

def prefetch_competition(competition, sport, today):
    """One Claude call fetches all TV matches for a competition and caches each team."""
    try:
        prompt = COMPETITION_PREFETCH_PROMPT.format(
            today=today, competition=competition, sport=sport
        )
        data = call_claude(prompt)
        matches = data.get('matches', [])
        if not matches:
            print(f"No matches today: {competition}")
            return
        for match in matches:
            match['playing_today'] = True
            match['competition'] = competition
            for team_field in ['home_team', 'away_team']:
                team = (match.get(team_field) or '').strip()
                if team:
                    set_cache(team, match)
                    print(f"  Cached: {team}")
    except Exception as e:
        print(f"Pre-fetch error ({competition}): {e}")

def prefetch():
    """Morning job: cache sport listings then all competition matches in parallel."""
    today = datetime.now().strftime('%A %d %B %Y')
    print(f"=== PRE-FETCH START: {today} ===")

    # Sport listings first
    for sport in ['football', 'rugby']:
        try:
            basil_sport(sport)
            print(f"Cached {sport} listings")
        except Exception as e:
            print(f"Error caching {sport}: {e}")
        time.sleep(3)

    # All competitions in parallel - max 2 at once to stay within rate limits
    with ThreadPoolExecutor(max_workers=2) as executor:
        for competition, sport in PREFETCH_COMPETITIONS:
            executor.submit(prefetch_competition, competition, sport, today)

    print("=== PRE-FETCH COMPLETE ===")

scheduler = BackgroundScheduler(timezone='Europe/London')
scheduler.add_job(prefetch, 'cron', hour=9, minute=0)
scheduler.start()

# ── Admin ─────────────────────────────────────────────────────────────────────

def auth_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get('admin'):
            return redirect('/admin/login')
        return f(*a, **kw)
    return wrapper

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    err = ''
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect('/admin')
        err = 'Wrong password.'
    return render_template_string(LOGIN_HTML, err=err)

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect('/admin/login')

@app.route('/admin')
@auth_required
def admin():
    users   = db.table('users').select('*').order('registered_at', desc=True).execute().data
    queries = db.table('queries').select('*').order('queried_at', desc=True).limit(100).execute().data
    codes   = db.table('invite_codes').select('*').order('created_at', desc=True).execute().data
    today_count = sum(1 for q in queries if q['queried_at'][:10] == date.today().isoformat())
    return render_template_string(ADMIN_HTML,
        users=users, queries=queries, codes=codes,
        today_count=today_count,
        total_users=len(users),
        total_queries=len(queries)
    )

@app.route('/admin/block', methods=['POST'])
@auth_required
def block_user():
    db.table('users').update({'blocked': True}).eq('phone_number', request.form.get('phone')).execute()
    return redirect('/admin')

@app.route('/admin/unblock', methods=['POST'])
@auth_required
def unblock_user():
    db.table('users').update({'blocked': False}).eq('phone_number', request.form.get('phone')).execute()
    return redirect('/admin')

@app.route('/admin/generate', methods=['POST'])
@auth_required
def generate_code():
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    db.table('invite_codes').insert({
        'code':       code,
        'max_uses':   int(request.form.get('max_uses', 1)),
        'uses_count': 0,
        'note':       request.form.get('note', ''),
        'created_at': datetime.utcnow().isoformat()
    }).execute()
    return redirect('/admin')

@app.route('/admin/delete-code', methods=['POST'])
@auth_required
def delete_code():
    db.table('invite_codes').delete().eq('code', request.form.get('code')).execute()
    return redirect('/admin')

@app.route('/admin/prefetch', methods=['POST'])
@auth_required
def admin_prefetch():
    threading.Thread(target=prefetch, daemon=True).start()
    return redirect('/admin')

# ── HTML ──────────────────────────────────────────────────────────────────────

LOGIN_HTML = '''<!doctype html>
<html>
<head>
  <title>Basil Admin</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:system-ui,sans-serif;background:#f4f1eb;
         display:flex;align-items:center;justify-content:center;min-height:100vh}
    .card{background:white;padding:2.5rem;border-radius:14px;
          box-shadow:0 4px 24px rgba(0,0,0,.1);width:100%;max-width:380px}
    h1{font-size:2rem;margin-bottom:.2rem}
    p{color:#888;margin-bottom:1.5rem;font-size:.9rem}
    input{width:100%;padding:.75rem 1rem;border:1px solid #ddd;
          border-radius:8px;font-size:1rem;margin-bottom:1rem}
    button{width:100%;padding:.75rem;background:#d4720a;color:white;
           border:none;border-radius:8px;font-size:1rem;cursor:pointer;font-weight:600}
    button:hover{background:#b85e08}
    .err{color:#c00;font-size:.85rem;margin-bottom:1rem}
  </style>
</head>
<body>
  <div class="card">
    <h1>🦊 Basil</h1>
    <p>Admin console</p>
    {% if err %}<div class="err">{{ err }}</div>{% endif %}
    <form method="post">
      <input type="password" name="password" placeholder="Password" autofocus>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>'''

ADMIN_HTML = '''<!doctype html>
<html>
<head>
  <title>Basil Admin</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:system-ui,sans-serif;background:#f4f1eb;color:#222}
    header{background:#d4720a;color:white;padding:1rem 2rem;
           display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap}
    header h1{font-size:1.4rem}
    header div{display:flex;gap:.75rem;align-items:center}
    header a{color:white;text-decoration:none;font-size:.85rem;opacity:.8}
    .prefetch-btn{background:rgba(255,255,255,.2);color:white;border:none;
                  padding:.4rem .9rem;border-radius:6px;cursor:pointer;font-size:.8rem;font-weight:600}
    .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;padding:1.5rem 2rem}
    .stat{background:white;border-radius:10px;padding:1.25rem;box-shadow:0 2px 8px rgba(0,0,0,.07)}
    .stat .num{font-size:2rem;font-weight:700;color:#d4720a}
    .stat .lbl{font-size:.8rem;color:#888;margin-top:.2rem}
    .tabs{display:flex;gap:.5rem;padding:0 2rem;border-bottom:2px solid #e0d9cf}
    .tab{padding:.75rem 1.25rem;cursor:pointer;font-weight:600;color:#888;
         border-bottom:3px solid transparent;margin-bottom:-2px;font-size:.9rem}
    .tab.active{color:#d4720a;border-color:#d4720a}
    .panel{display:none;padding:1.5rem 2rem}
    .panel.active{display:block}
    table{width:100%;border-collapse:collapse;background:white;
          border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.07)}
    th{background:#f9f6f1;padding:.75rem 1rem;text-align:left;
       font-size:.75rem;color:#888;text-transform:uppercase;letter-spacing:.05em}
    td{padding:.75rem 1rem;border-top:1px solid #f0ebe3;font-size:.875rem;vertical-align:top}
    tr:hover td{background:#faf8f5}
    .badge{display:inline-block;padding:.2rem .6rem;border-radius:20px;font-size:.75rem;font-weight:600}
    .ok{background:#d1fae5;color:#065f46}
    .blocked{background:#fee2e2;color:#991b1b}
    .code-badge{background:#fef3c7;color:#92400e;font-family:monospace;font-size:.85rem}
    .btn{padding:.35rem .8rem;border:none;border-radius:6px;cursor:pointer;font-size:.8rem;font-weight:600}
    .btn-red{background:#fee2e2;color:#991b1b}
    .btn-green{background:#d1fae5;color:#065f46}
    .btn-grey{background:#f3f4f6;color:#555}
    .card{background:white;border-radius:10px;padding:1.5rem;
          box-shadow:0 2px 8px rgba(0,0,0,.07);max-width:480px;margin-bottom:1.5rem}
    .card h3{margin-bottom:1rem;font-size:1rem}
    .row{display:flex;gap:.75rem;margin-bottom:.75rem;align-items:center}
    .row input,.row select{padding:.6rem .9rem;border:1px solid #ddd;border-radius:8px;
                            font-size:.9rem;flex:1}
    .btn-primary{background:#d4720a;color:white;padding:.6rem 1.25rem;border:none;
                 border-radius:8px;cursor:pointer;font-weight:600;font-size:.9rem;white-space:nowrap}
    .trunc{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .muted{color:#aaa;font-size:.8rem}
    .two-col{display:grid;grid-template-columns:1fr 2fr;gap:1.5rem;align-items:start}
  </style>
</head>
<body>
<header>
  <h1>🦊 Basil Admin</h1>
  <div>
    <form method="post" action="/admin/prefetch" style="display:inline">
      <button class="prefetch-btn">↻ Pre-fetch today</button>
    </form>
    <a href="/admin/logout">Sign out</a>
  </div>
</header>
<div class="stats">
  <div class="stat"><div class="num">{{ total_users }}</div><div class="lbl">Registered users</div></div>
  <div class="stat"><div class="num">{{ today_count }}</div><div class="lbl">Queries today</div></div>
  <div class="stat"><div class="num">{{ total_queries }}</div><div class="lbl">Total queries</div></div>
</div>
<div class="tabs">
  <div class="tab active" onclick="show('users',this)">Users ({{ total_users }})</div>
  <div class="tab" onclick="show('queries',this)">Query log</div>
  <div class="tab" onclick="show('codes',this)">Invite codes</div>
</div>
<div id="users" class="panel active">
  <table>
    <thead><tr><th>Phone</th><th>Code</th><th>Registered</th><th>Status</th><th></th></tr></thead>
    <tbody>
    {% for u in users %}
    <tr>
      <td>{{ u.phone_number }}</td>
      <td><span class="badge code-badge">{{ u.invite_code }}</span></td>
      <td>{{ u.registered_at[:16].replace("T"," ") }}</td>
      <td>{% if u.blocked %}<span class="badge blocked">Blocked</span>{% else %}<span class="badge ok">Active</span>{% endif %}</td>
      <td>
        {% if u.blocked %}
          <form method="post" action="/admin/unblock" style="display:inline">
            <input type="hidden" name="phone" value="{{ u.phone_number }}">
            <button class="btn btn-green">Unblock</button>
          </form>
        {% else %}
          <form method="post" action="/admin/block" style="display:inline">
            <input type="hidden" name="phone" value="{{ u.phone_number }}">
            <button class="btn btn-red">Block</button>
          </form>
        {% endif %}
      </td>
    </tr>
    {% else %}
    <tr><td colspan="5" style="text-align:center;color:#aaa;padding:2rem">No users yet</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>
<div id="queries" class="panel">
  <table>
    <thead><tr><th>Time</th><th>Phone</th><th>Query</th><th>Response preview</th></tr></thead>
    <tbody>
    {% for q in queries %}
    <tr>
      <td style="white-space:nowrap">{{ q.queried_at[:16].replace("T"," ") }}</td>
      <td>{{ q.phone_number }}</td>
      <td class="trunc">{{ q.query }}</td>
      <td class="trunc muted">{{ q.response }}</td>
    </tr>
    {% else %}
    <tr><td colspan="4" style="text-align:center;color:#aaa;padding:2rem">No queries yet</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>
<div id="codes" class="panel">
  <div class="two-col">
    <div class="card">
      <h3>Generate invite code</h3>
      <form method="post" action="/admin/generate">
        <div class="row"><input name="note" placeholder="Who's this for? (optional)"></div>
        <div class="row">
          <select name="max_uses">
            <option value="1">1 use</option>
            <option value="3">3 uses</option>
            <option value="5">5 uses</option>
            <option value="10">10 uses</option>
            <option value="999">Unlimited</option>
          </select>
          <button type="submit" class="btn-primary">Generate</button>
        </div>
      </form>
    </div>
    <table>
      <thead><tr><th>Code</th><th>Note</th><th>Uses</th><th></th></tr></thead>
      <tbody>
      {% for c in codes %}
      <tr>
        <td><span class="badge code-badge">{{ c.code }}</span></td>
        <td>{{ c.note or "-" }}</td>
        <td>{{ c.uses_count }} / {{ c.max_uses }}</td>
        <td>
          <form method="post" action="/admin/delete-code" style="display:inline">
            <input type="hidden" name="code" value="{{ c.code }}">
            <button class="btn btn-grey">Delete</button>
          </form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="4" style="text-align:center;color:#aaa;padding:2rem">No codes yet</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
<script>
function show(id, tab) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  tab.classList.add('active');
}
</script>
</body>
</html>'''

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
