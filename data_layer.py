# -*- coding: utf-8 -*-
"""
Basil data layer - replaces Claude web search with dedicated APIs.

Priority chain per query:
  Fixtures : API-Football / API-Rugby  →  WheresTheMatch scrape  →  Claude web search
  Odds     : The Odds API              →  API-Football odds      →  Claude web search
  TV       : WheresTheMatch scrape     →  Claude web search
  Fox fact : Claude Sonnet (NO web search, tiny prompt)

Cost reduction: ~85-90% vs previous all-Claude-web-search approach.
"""

import os
import re
import json
import time
import requests
from datetime import date, datetime
from fractions import Fraction

# ── API credentials ──────────────────────────────────────────────────────────

APISPORTS_KEY = os.environ.get('APISPORTS_KEY', '')   # API-Football + API-Rugby
ODDS_API_KEY  = os.environ.get('ODDS_API_KEY', '')    # The Odds API

FOOTBALL_BASE = 'https://v3.football.api-sports.io'
RUGBY_BASE    = 'https://v1.rugby.api-sports.io'
ODDS_BASE     = 'https://api.the-odds-api.com/v4'

APISPORTS_HEADERS = {'x-apisports-key': APISPORTS_KEY}

# ── Odds API sport keys for UK-relevant competitions ─────────────────────────

FOOTBALL_SPORT_KEYS = [
    'soccer_epl',
    'soccer_scotland_premiership',
    'soccer_uefa_champs_league',
    'soccer_fa_cup',
    'soccer_scotland_fa_cup',
]

RUGBY_SPORT_KEYS = [
    'rugbyunion_premiership',
    'rugbyunion_united_rugby_championship',
    'rugbyunion_champions_cup',
    'rugbyunion_challenge_cup',
    'rugbyunion_six_nations',
    'rugbyunion_super_rugby',
    'rugbyunion_world_cup',
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def decimal_to_fractional(d):
    """Convert decimal odds (e.g. 4.0) to fractional string (e.g. 3/1)."""
    try:
        d = float(d)
        if d <= 1:
            return 'N/A'
        if abs(d - 2.0) < 0.01:
            return 'Evs'
        frac = Fraction(d - 1).limit_denominator(20)
        return f"{frac.numerator}/{frac.denominator}"
    except Exception:
        return str(d)

def today_str():
    return date.today().isoformat()

def team_matches(name, fixture):
    """Check if team name loosely matches a fixture team."""
    name = name.lower().strip()
    home = (fixture.get('home_team') or '').lower()
    away = (fixture.get('away_team') or '').lower()
    return name in home or name in away or home in name or away in name

# ── API-Football ──────────────────────────────────────────────────────────────

def api_football_fixtures(team_name):
    """Search today's football fixtures by team name. Returns list of fixture dicts."""
    if not APISPORTS_KEY:
        return []
    try:
        resp = requests.get(
            f'{FOOTBALL_BASE}/fixtures',
            headers=APISPORTS_HEADERS,
            params={'date': today_str(), 'timezone': 'Europe/London'},
            timeout=8
        )
        data = resp.json()
        results = []
        for item in data.get('response', []):
            teams = item.get('teams', {})
            home  = teams.get('home', {}).get('name', '')
            away  = teams.get('away', {}).get('name', '')
            name_lower = team_name.lower()
            if name_lower in home.lower() or name_lower in away.lower():
                fixture = item.get('fixture', {})
                league  = item.get('league', {})
                venue   = item.get('fixture', {}).get('venue', {})
                results.append({
                    'fixture_id':  fixture.get('id'),
                    'home_team':   home,
                    'away_team':   away,
                    'competition': league.get('name', ''),
                    'venue':       venue.get('name', '') if isinstance(venue, dict) else '',
                    'kickoff':     fixture.get('date', '')[:16].replace('T', ' ')[11:16],
                    'sport':       'football',
                    'playing_today': True,
                })
        # If not today, try next fixture
        if not results:
            resp2 = requests.get(
                f'{FOOTBALL_BASE}/fixtures',
                headers=APISPORTS_HEADERS,
                params={'search': team_name, 'next': 1, 'timezone': 'Europe/London'},
                timeout=8
            )
            for item in resp2.json().get('response', []):
                teams = item.get('teams', {})
                home  = teams.get('home', {}).get('name', '')
                away  = teams.get('away', {}).get('name', '')
                fixture = item.get('fixture', {})
                league  = item.get('league', {})
                venue   = item.get('fixture', {}).get('venue', {})
                dt = fixture.get('date', '')
                next_date = 'Tomorrow' if dt[:10] == (date.today().isoformat()[:10]) else dt[:10]
                results.append({
                    'fixture_id':  fixture.get('id'),
                    'home_team':   home,
                    'away_team':   away,
                    'competition': league.get('name', ''),
                    'venue':       venue.get('name', '') if isinstance(venue, dict) else '',
                    'kickoff':     dt[11:16] if len(dt) > 11 else '',
                    'next_date':   next_date,
                    'sport':       'football',
                    'playing_today': False,
                })
        return results
    except Exception as e:
        print(f"API-Football error: {e}")
        return []

# ── API-Rugby ─────────────────────────────────────────────────────────────────

def api_rugby_fixtures(team_name):
    """Search today's rugby fixtures by team name."""
    if not APISPORTS_KEY:
        return []
    try:
        resp = requests.get(
            f'{RUGBY_BASE}/games',
            headers=APISPORTS_HEADERS,
            params={'date': today_str(), 'timezone': 'Europe/London'},
            timeout=8
        )
        data = resp.json()
        results = []
        name_lower = team_name.lower()
        for item in data.get('response', []):
            teams = item.get('teams', {})
            home  = teams.get('home', {}).get('name', '')
            away  = teams.get('away', {}).get('name', '')
            if name_lower in home.lower() or name_lower in away.lower():
                league = item.get('league', {})
                venue  = item.get('venue', {})
                status = item.get('status', {})
                dt     = item.get('date', '')
                results.append({
                    'fixture_id':  item.get('id'),
                    'home_team':   home,
                    'away_team':   away,
                    'competition': league.get('name', ''),
                    'venue':       venue.get('name', '') if isinstance(venue, dict) else '',
                    'kickoff':     dt[11:16] if len(dt) > 11 else '',
                    'sport':       'rugby',
                    'playing_today': True,
                })
        # If not today, try upcoming
        if not results:
            resp2 = requests.get(
                f'{RUGBY_BASE}/games',
                headers=APISPORTS_HEADERS,
                params={'team': team_name, 'next': 1, 'timezone': 'Europe/London'},
                timeout=8
            )
            for item in resp2.json().get('response', []):
                teams = item.get('teams', {})
                home  = teams.get('home', {}).get('name', '')
                away  = teams.get('away', {}).get('name', '')
                league = item.get('league', {})
                venue  = item.get('venue', {})
                dt     = item.get('date', '')
                next_date = 'Tomorrow' if dt[:10] == date.today().isoformat() else dt[:10]
                results.append({
                    'fixture_id':  item.get('id'),
                    'home_team':   home,
                    'away_team':   away,
                    'competition': league.get('name', ''),
                    'venue':       venue.get('name', '') if isinstance(venue, dict) else '',
                    'kickoff':     dt[11:16] if len(dt) > 11 else '',
                    'next_date':   next_date,
                    'sport':       'rugby',
                    'playing_today': False,
                })
        return results
    except Exception as e:
        print(f"API-Rugby error: {e}")
        return []

# ── The Odds API ──────────────────────────────────────────────────────────────

def fetch_odds(home_team, away_team, sport):
    """
    Fetch odds from The Odds API for the given match.
    Returns dict with home_odds, draw_odds, away_odds, bookmaker or None.
    """
    if not ODDS_API_KEY:
        return None
    sport_keys = FOOTBALL_SPORT_KEYS if sport == 'football' else RUGBY_SPORT_KEYS
    name_lower = home_team.lower()

    for sport_key in sport_keys:
        try:
            resp = requests.get(
                f'{ODDS_BASE}/sports/{sport_key}/odds/',
                params={
                    'apiKey':  ODDS_API_KEY,
                    'regions': 'uk',
                    'markets': 'h2h',
                    'oddsFormat': 'decimal',
                },
                timeout=8
            )
            if resp.status_code == 422:
                continue  # sport key not valid/active
            events = resp.json()
            for event in (events if isinstance(events, list) else []):
                ht = (event.get('home_team') or '').lower()
                at = (event.get('away_team') or '').lower()
                if (name_lower in ht or ht in name_lower or
                    name_lower in at or at in name_lower):
                    # Found matching event - extract best UK bookmaker odds
                    bookmakers_pref = ['paddypower', 'williamhill', 'bet365', 'betfair']
                    bookmakers = event.get('bookmakers', [])
                    chosen = None
                    for pref in bookmakers_pref:
                        for bk in bookmakers:
                            if pref in bk.get('key', '').lower():
                                chosen = bk
                                break
                        if chosen:
                            break
                    if not chosen and bookmakers:
                        chosen = bookmakers[0]
                    if not chosen:
                        continue

                    outcomes = {}
                    for market in chosen.get('markets', []):
                        if market.get('key') == 'h2h':
                            for o in market.get('outcomes', []):
                                outcomes[o['name'].lower()] = o['price']

                    # Map outcomes to home/draw/away
                    home_price = outcomes.get(event.get('home_team','').lower())
                    away_price = outcomes.get(event.get('away_team','').lower())
                    draw_price = outcomes.get('draw')

                    bk_name_map = {
                        'paddypower': 'Paddy Power',
                        'williamhill': 'William Hill',
                        'bet365': 'Bet365',
                        'betfair': 'Betfair',
                    }
                    bk_key  = chosen.get('key', '')
                    bk_name = bk_name_map.get(bk_key, chosen.get('title', 'Paddy Power'))
                    bk_url_map = {
                        'Paddy Power':  'https://www.paddypower.com',
                        'William Hill': 'https://www.williamhill.com',
                        'Bet365':       'https://www.bet365.com',
                        'Betfair':      'https://www.betfair.com',
                    }

                    return {
                        'home_odds':    decimal_to_fractional(home_price) if home_price else '',
                        'draw_odds':    decimal_to_fractional(draw_price) if draw_price else '',
                        'away_odds':    decimal_to_fractional(away_price) if away_price else '',
                        'bookmaker':    bk_name,
                        'bookmaker_url': bk_url_map.get(bk_name, 'https://www.paddypower.com'),
                    }
        except Exception as e:
            print(f"Odds API error ({sport_key}): {e}")
            continue
    return None

# ── WheresTheMatch scraper ────────────────────────────────────────────────────

WTM_FOOTBALL = 'https://www.wheresthematch.com/live-football-on-tv/'
WTM_RUGBY    = 'https://www.wheresthematch.com/live-rugby-union-on-tv/'

def scrape_wheresthematch(team_name, sport):
    """
    Try to scrape WheresTheMatch for TV channel info.
    Returns tv_channel string or None if not found / JS-rendered.
    """
    try:
        url = WTM_FOOTBALL if sport == 'football' else WTM_RUGBY
        resp = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; BasilBot/1.0)'
        })
        if resp.status_code != 200:
            return None
        html = resp.text
        # Look for team name near a channel name
        name_lower = team_name.lower()
        # Find all text blocks containing the team name
        idx = html.lower().find(name_lower)
        if idx == -1:
            return None
        # Extract surrounding 500 chars and look for channel names
        snippet = html[max(0, idx-200):idx+500]
        channels = [
            'Sky Sports', 'Premier Sports', 'TNT Sports', 'BBC Two', 'ITV',
            'Channel 4', 'S4C', 'BBC One', 'Amazon Prime', 'DAZN',
            'BBC Alba', 'TG4', 'Virgin Media', 'FreeSports'
        ]
        for ch in channels:
            if ch.lower() in snippet.lower():
                return ch
        return None
    except Exception as e:
        print(f"WheresTheMatch scrape error: {e}")
        return None

# ── Fox fact generator (Claude, NO web search) ────────────────────────────────

FOX_FACT_PROMPT = """\
You are Basil - a dry, witty fox. Write a fox fact about this match.

Match: {home_team} vs {away_team}
Competition: {competition}
{odds_line}

Rules:
- Must be genuinely surprising real fox behaviour (not made up)
- Under 60 words
- End with a dry, knowing one-liner connecting the fox behaviour to this match or its odds
- Never use exclamation marks
- Tone: dry, precise, occasionally theatrical - never laddish

Return ONLY the fox fact text. Nothing else."""

def generate_fox_fact(match):
    """Generate fox fact using Claude Sonnet WITHOUT web search. Very cheap."""
    import anthropic as _anthropic
    claude = _anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
    odds_line = ''
    if match.get('home_odds'):
        odds_line = f"Odds: {match['home_team']} {match['home_odds']} | Draw {match.get('draw_odds','')} | {match['away_team']} {match.get('away_odds','')}"
    prompt = FOX_FACT_PROMPT.format(
        home_team   = match.get('home_team', ''),
        away_team   = match.get('away_team', ''),
        competition = match.get('competition', ''),
        odds_line   = odds_line,
    )
    try:
        resp = claude.messages.create(
            model='claude-haiku-4-5-20251001',  # Haiku - even cheaper for this simple task
            max_tokens=200,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return (resp.content[0].text or '').strip()
    except Exception as e:
        print(f"Fox fact error: {e}")
        return "Foxes are one of the few wild animals known to play purely for fun. Worth remembering when you're studying the odds."

# ── Sport detector ────────────────────────────────────────────────────────────

SPORT_DETECT_PROMPT = """\
Classify this sports team query. Reply with exactly one word.

Query: "{query}"

football  - if this is clearly a football (soccer) team
rugby     - if this is clearly a rugby union team
both      - if it could be either sport (very rare)
unknown   - if genuinely unclear

One word only."""

def detect_sport(query):
    """Use Claude Haiku to detect sport. Returns 'football', 'rugby', 'both', 'unknown'."""
    import anthropic as _anthropic
    claude = _anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
    # Fast heuristics first (no API call needed)
    q = query.lower().strip()
    rugby_hints    = ['rugby', 'rfc', 'munster', 'leinster', 'ulster', 'connacht',
                      'wasps', 'bath rugby', 'exeter', 'northampton saints',
                      'saracens', 'harlequins', 'leicester tigers', 'toulon',
                      'clermont', 'la rochelle', 'stormers', 'sharks', 'blues',
                      'crusaders', 'chiefs', 'highlanders', 'brumbies']
    football_hints = ['fc', 'united', 'city', 'town', 'wanderers', 'rovers',
                      'athletic', 'albion', 'arsenal', 'chelsea', 'liverpool',
                      'celtic', 'rangers', 'hibs', 'hearts', 'dundee',
                      'aberdeen', 'motherwell', 'kilmarnock', 'real madrid',
                      'barcelona', 'juventus', 'psg', 'ajax', 'porto']
    for hint in rugby_hints:
        if hint in q:
            return 'rugby'
    for hint in football_hints:
        if hint in q:
            return 'football'
    # Fall back to Haiku
    try:
        resp = claude.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=5,
            messages=[{'role': 'user', 'content': SPORT_DETECT_PROMPT.format(query=query)}]
        )
        result = (resp.content[0].text or '').strip().lower().split()[0]
        return result if result in ('football', 'rugby', 'both', 'unknown') else 'unknown'
    except Exception as e:
        print(f"Sport detect error: {e}")
        return 'unknown'

# ── Main orchestrator ─────────────────────────────────────────────────────────

def get_match_data(team_name, sport=None):
    """
    Full data fetch pipeline with fallbacks.
    Returns a match dict compatible with fmt_team() or None on total failure.
    """
    if not sport or sport == 'unknown':
        sport = detect_sport(team_name)
    if sport == 'both':
        sport = 'rugby'  # default for ambiguous (Saints etc handled by disambiguation)

    print(f"get_match_data: team={team_name}, sport={sport}")

    # 1. Get fixture from API
    fixtures = []
    if sport == 'football':
        fixtures = api_football_fixtures(team_name)
    else:
        fixtures = api_rugby_fixtures(team_name)

    if not fixtures:
        print(f"No fixtures from API for {team_name}")
        return None  # Caller falls back to Claude web search

    match = fixtures[0]

    # Ensure searched team appears first
    name_lower = team_name.lower()
    if name_lower in match.get('away_team', '').lower() and \
       name_lower not in match.get('home_team', '').lower():
        match['home_team'], match['away_team'] = match['away_team'], match['home_team']
        # Mark as away so venue can be labelled correctly
        if match.get('venue'):
            match['venue'] = f"Away at {match['venue']}"

    # 2. Get odds
    odds = fetch_odds(match['home_team'], match['away_team'], sport)
    if odds:
        match.update(odds)
    else:
        print(f"No odds found for {match['home_team']} vs {match['away_team']}")
        match.update({'home_odds': '', 'draw_odds': '', 'away_odds': '',
                      'bookmaker': '', 'bookmaker_url': ''})

    # 3. TV channel from WheresTheMatch scrape
    tv = scrape_wheresthematch(team_name, sport)
    match['tv_channel']    = tv or ''
    match['radio_station'] = ''
    match['coverage_start'] = ''

    # 4. Fox fact (Claude Haiku, no web search)
    match['fox_fact'] = generate_fox_fact(match)

    return match
