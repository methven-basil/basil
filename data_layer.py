# -*- coding: utf-8 -*-
"""
Basil data layer - dedicated sports APIs replace Claude web search.

Priority chain:
  Fixtures : API-Football/Rugby (2-step: team ID -> fixture) -> Claude web search fallback
  Odds     : The Odds API (search all relevant sport keys)   -> Claude web search fallback
  TV       : WheresTheMatch scrape                           -> Claude targeted Haiku call
  Fox fact : Claude Haiku, NO web search (~300 tokens)

Cost vs old approach: ~95% reduction in tokens/web searches.
"""

import os
import re
import json
import time
import requests
from datetime import date, datetime, timedelta
from fractions import Fraction

# ── Credentials ───────────────────────────────────────────────────────────────

APISPORTS_KEY = os.environ.get('APISPORTS_KEY', '')
ODDS_API_KEY  = os.environ.get('ODDS_API_KEY', '')

FOOTBALL_BASE = 'https://v3.football.api-sports.io'
RUGBY_BASE    = 'https://v1.rugby.api-sports.io'
ODDS_BASE     = 'https://api.the-odds-api.com/v4'
HEADERS       = {'x-apisports-key': APISPORTS_KEY}

# Odds API sport keys to check for UK-relevant competitions
FOOTBALL_SPORT_KEYS = [
    'soccer_epl', 'soccer_scotland_premiership', 'soccer_uefa_champs_league',
    'soccer_fa_cup', 'soccer_scotland_fa_cup', 'soccer_england_league1',
    'soccer_england_league2', 'soccer_spain_la_liga', 'soccer_italy_serie_a',
    'soccer_germany_bundesliga', 'soccer_france_ligue_one',
]
RUGBY_SPORT_KEYS = [
    'rugbyunion_premiership', 'rugbyunion_united_rugby_championship',
    'rugbyunion_champions_cup', 'rugbyunion_challenge_cup',
    'rugbyunion_six_nations', 'rugbyunion_super_rugby', 'rugbyunion_world_cup',
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def decimal_to_fractional(d):
    """Convert decimal odds (4.0) to fractional string (3/1)."""
    try:
        d = float(d)
        if d <= 1:
            return 'N/A'
        if abs(d - 2.0) < 0.05:
            return 'Evs'
        frac = Fraction(d - 1).limit_denominator(20)
        return f"{frac.numerator}/{frac.denominator}"
    except Exception:
        return str(d)

def fmt_kickoff(dt_str):
    """Extract HH:MM from ISO datetime string."""
    if not dt_str:
        return ''
    try:
        # Handle both '2026-05-05T19:45:00+01:00' and '2026-05-05T19:45:00'
        return dt_str[11:16]
    except Exception:
        return ''

def fmt_date(dt_str):
    """Return 'Tomorrow' or formatted date string."""
    if not dt_str:
        return 'TBC'
    try:
        d = dt_str[:10]
        today    = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        if d == today:
            return 'Today'
        if d == tomorrow:
            return 'Tomorrow'
        # Format as 'Saturday 9 May 2026'
        dt = datetime.strptime(d, '%Y-%m-%d')
        return dt.strftime('%A %d %B %Y')
    except Exception:
        return dt_str[:10]

def name_matches(search, candidate):
    """Fuzzy team name match."""
    s = search.lower().strip()
    c = candidate.lower().strip()
    return s in c or c in s or s[:6] in c

# ── Step 1: Get team ID ────────────────────────────────────────────────────────

def get_football_team_id(team_name):
    """Look up API-Football team ID by name. Returns int or None."""
    try:
        resp = requests.get(
            f'{FOOTBALL_BASE}/teams',
            headers=HEADERS,
            params={'search': team_name},
            timeout=8
        )
        teams = resp.json().get('response', [])
        if teams:
            print(f"API-Football team found: {teams[0]['team']['name']} (id={teams[0]['team']['id']})")
            return teams[0]['team']['id']
    except Exception as e:
        print(f"Football team ID error: {e}")
    return None

def get_rugby_team_id(team_name):
    """Look up API-Rugby team ID by name. Returns int or None."""
    try:
        resp = requests.get(
            f'{RUGBY_BASE}/teams',
            headers=HEADERS,
            params={'search': team_name},
            timeout=8
        )
        teams = resp.json().get('response', [])
        if teams:
            print(f"API-Rugby team found: {teams[0]['name']} (id={teams[0]['id']})")
            return teams[0]['id']
    except Exception as e:
        print(f"Rugby team ID error: {e}")
    return None

# ── Step 2: Get fixtures ───────────────────────────────────────────────────────

def get_football_fixture(team_id, team_name):
    """
    Try today's fixture first, then next upcoming.
    Returns fixture dict or None.
    """
    today = date.today().isoformat()
    try:
        # Try today
        resp = requests.get(
            f'{FOOTBALL_BASE}/fixtures',
            headers=HEADERS,
            params={'team': team_id, 'date': today, 'timezone': 'Europe/London'},
            timeout=8
        )
        fixtures = resp.json().get('response', [])
        if fixtures:
            return _parse_football_fixture(fixtures[0], playing_today=True)

        # Try next fixture
        resp2 = requests.get(
            f'{FOOTBALL_BASE}/fixtures',
            headers=HEADERS,
            params={'team': team_id, 'next': 1, 'timezone': 'Europe/London'},
            timeout=8
        )
        fixtures2 = resp2.json().get('response', [])
        if fixtures2:
            return _parse_football_fixture(fixtures2[0], playing_today=False)

    except Exception as e:
        print(f"Football fixture error: {e}")
    return None

def _parse_football_fixture(item, playing_today):
    teams   = item.get('teams', {})
    fixture = item.get('fixture', {})
    league  = item.get('league', {})
    venue   = fixture.get('venue', {})
    dt      = fixture.get('date', '')
    home    = teams.get('home', {}).get('name', '')
    away    = teams.get('away', {}).get('name', '')
    return {
        'fixture_id':   fixture.get('id'),
        'home_team':    home,
        'away_team':    away,
        'competition':  league.get('name', ''),
        'venue':        venue.get('name', '') if isinstance(venue, dict) else '',
        'kickoff':      fmt_kickoff(dt),
        'next_date':    fmt_date(dt),
        'sport':        'football',
        'playing_today': playing_today,
    }

def get_rugby_fixture(team_id, team_name):
    """Try today then next upcoming rugby fixture."""
    today = date.today().isoformat()
    try:
        resp = requests.get(
            f'{RUGBY_BASE}/games',
            headers=HEADERS,
            params={'team': team_id, 'date': today, 'timezone': 'Europe/London'},
            timeout=8
        )
        games = resp.json().get('response', [])
        if games:
            return _parse_rugby_fixture(games[0], playing_today=True)

        # Next game
        resp2 = requests.get(
            f'{RUGBY_BASE}/games',
            headers=HEADERS,
            params={'team': team_id, 'next': 1, 'timezone': 'Europe/London'},
            timeout=8
        )
        games2 = resp2.json().get('response', [])
        if games2:
            return _parse_rugby_fixture(games2[0], playing_today=False)

    except Exception as e:
        print(f"Rugby fixture error: {e}")
    return None

def _parse_rugby_fixture(item, playing_today):
    teams  = item.get('teams', {})
    league = item.get('league', {})
    venue  = item.get('venue', {})
    dt     = item.get('date', '')
    home   = teams.get('home', {}).get('name', '')
    away   = teams.get('away', {}).get('name', '')
    return {
        'fixture_id':   item.get('id'),
        'home_team':    home,
        'away_team':    away,
        'competition':  league.get('name', ''),
        'venue':        venue.get('name', '') if isinstance(venue, dict) else '',
        'kickoff':      fmt_kickoff(dt),
        'next_date':    fmt_date(dt),
        'sport':        'rugby',
        'playing_today': playing_today,
    }

# ── Odds ──────────────────────────────────────────────────────────────────────

BK_PRIORITY = ['paddypower', 'williamhill', 'bet365', 'betfair', 'unibet']
BK_NAMES    = {
    'paddypower':  'Paddy Power',
    'williamhill': 'William Hill',
    'bet365':      'Bet365',
    'betfair':     'Betfair',
    'unibet':      'Unibet',
}
BK_URLS = {
    'Paddy Power':  'https://www.paddypower.com',
    'William Hill': 'https://www.williamhill.com',
    'Bet365':       'https://www.bet365.com',
    'Betfair':      'https://www.betfair.com',
    'Unibet':       'https://www.unibet.co.uk',
}

def fetch_odds(home_team, away_team, sport):
    """Search The Odds API across all relevant sport keys."""
    if not ODDS_API_KEY:
        return None
    sport_keys = FOOTBALL_SPORT_KEYS if sport == 'football' else RUGBY_SPORT_KEYS

    for sport_key in sport_keys:
        try:
            resp = requests.get(
                f'{ODDS_BASE}/sports/{sport_key}/odds/',
                params={
                    'apiKey':     ODDS_API_KEY,
                    'regions':    'uk',
                    'markets':    'h2h',
                    'oddsFormat': 'decimal',
                },
                timeout=8
            )
            if resp.status_code in (404, 422):
                continue
            events = resp.json()
            if not isinstance(events, list):
                continue

            for event in events:
                ht = (event.get('home_team') or '').lower()
                at = (event.get('away_team') or '').lower()
                if name_matches(home_team, ht) or name_matches(home_team, at) or \
                   name_matches(away_team, ht) or name_matches(away_team, at):

                    bookmakers = event.get('bookmakers', [])
                    chosen = None
                    for pref in BK_PRIORITY:
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

                    home_price = outcomes.get(event.get('home_team', '').lower())
                    away_price = outcomes.get(event.get('away_team', '').lower())
                    draw_price = outcomes.get('draw')

                    bk_key  = chosen.get('key', '')
                    bk_name = BK_NAMES.get(bk_key, chosen.get('title', 'Paddy Power'))
                    print(f"Odds found via {bk_name} on {sport_key}")
                    return {
                        'home_odds':    decimal_to_fractional(home_price) if home_price else '',
                        'draw_odds':    decimal_to_fractional(draw_price) if draw_price else '',
                        'away_odds':    decimal_to_fractional(away_price) if away_price else '',
                        'bookmaker':    bk_name,
                        'bookmaker_url': BK_URLS.get(bk_name, 'https://www.paddypower.com'),
                    }
        except Exception as e:
            print(f"Odds API error ({sport_key}): {e}")
            continue
    print(f"No odds found for {home_team} vs {away_team}")
    return None

# ── WheresTheMatch TV scraper ─────────────────────────────────────────────────

WTM_FOOTBALL = 'https://www.wheresthematch.com/live-football-on-tv/'
WTM_RUGBY    = 'https://www.wheresthematch.com/live-rugby-union-on-tv/'

UK_CHANNELS = [
    'Sky Sports Main Event', 'Sky Sports Football', 'Sky Sports Premier League',
    'Sky Sports Action', 'Sky Sports Arena',
    'Premier Sports 1', 'Premier Sports 2',
    'TNT Sports 1', 'TNT Sports 2', 'TNT Sports 3', 'TNT Sports 4',
    'BBC One', 'BBC Two', 'BBC Three', 'ITV', 'ITV4', 'Channel 4',
    'Amazon Prime Video', 'DAZN', 'S4C', 'BBC Alba', 'TG4',
    'Virgin Media Sport', 'FreeSports',
]

def scrape_tv_channel(team_name, sport):
    """
    Try to scrape WheresTheMatch for TV channel.
    Returns channel string or None.
    """
    try:
        url = WTM_FOOTBALL if sport == 'football' else WTM_RUGBY
        resp = requests.get(url, timeout=10, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        if resp.status_code != 200:
            print(f"WheresTheMatch returned {resp.status_code}")
            return None
        html = resp.text
        name_lower = team_name.lower()
        idx = html.lower().find(name_lower)
        if idx == -1:
            print(f"WheresTheMatch: '{team_name}' not found in page")
            return None
        # Look in surrounding 600 chars for channel names
        snippet = html[max(0, idx-100):idx+600]
        # Sort channels longest-first to match most specific first
        for ch in sorted(UK_CHANNELS, key=len, reverse=True):
            if ch.lower() in snippet.lower():
                print(f"WheresTheMatch: found channel '{ch}' for '{team_name}'")
                return ch
        print(f"WheresTheMatch: team found but no channel identified in snippet")
        return None
    except Exception as e:
        print(f"WheresTheMatch scrape error: {e}")
        return None

# ── Fox fact (Claude Haiku, NO web search) ────────────────────────────────────

FOX_FACT_PROMPT = """\
You are Basil - a dry, witty fox who knows sport. Write a fox fact about this match.

{home_team} vs {away_team} | {competition}{odds_section}

Rules:
- Must describe real, verifiable fox behaviour (surprising or counterintuitive)
- Under 60 words total
- Final sentence must be a dry one-liner connecting the fox behaviour to this match or its odds
- Never use exclamation marks
- Tone: dry, economical, knowing - never laddish or pushy

Return ONLY the fox fact. No preamble, no label, just the text."""

def generate_fox_fact(match):
    """Generate fox fact using Claude Haiku WITHOUT web search. ~300 tokens."""
    import anthropic as _ant
    cl = _ant.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
    odds_section = ''
    if match.get('home_odds'):
        odds_section = (
            f"\nOdds: {match['home_team']} {match['home_odds']} | "
            f"Draw {match.get('draw_odds','')} | "
            f"{match['away_team']} {match.get('away_odds','')}"
        )
    prompt = FOX_FACT_PROMPT.format(
        home_team   = match.get('home_team', ''),
        away_team   = match.get('away_team', ''),
        competition = match.get('competition', 'Unknown competition'),
        odds_section= odds_section,
    )
    try:
        resp = cl.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=150,
            messages=[{'role': 'user', 'content': prompt}]
        )
        fact = (resp.content[0].text or '').strip()
        print(f"Fox fact generated ({len(fact)} chars)")
        return fact
    except Exception as e:
        print(f"Fox fact error: {e}")
        return ("Foxes cache food across hundreds of locations and remember each one. "
                "Whether that instinct helps with today's result is another matter.")

# ── Sport detector ────────────────────────────────────────────────────────────

# Fast heuristics - no API call needed
RUGBY_HINTS    = {'rugby', 'rfc', 'munster', 'leinster', 'ulster', 'connacht',
                  'wasps', 'bath rugby', 'exeter', 'northampton saints', 'saracens',
                  'harlequins', 'leicester tigers', 'toulon', 'clermont', 'la rochelle',
                  'stormers', 'sharks', 'blues', 'crusaders', 'chiefs', 'highlanders',
                  'brumbies', 'hurricanes', 'rebels', 'lions', 'reds', 'force',
                  'edinburgh', 'glasgow warriors', 'dragons', 'scarlets', 'ospreys',
                  'cardiff', 'zebre', 'benetton', 'bulls', 'cheetahs'}

FOOTBALL_HINTS = {'fc', 'united', 'city', 'town', 'wanderers', 'rovers', 'athletic',
                  'albion', 'arsenal', 'chelsea', 'liverpool', 'tottenham', 'spurs',
                  'celtic', 'rangers', 'hibs', 'hibernian', 'hearts', 'dundee',
                  'aberdeen', 'motherwell', 'kilmarnock', 'real madrid', 'barcelona',
                  'juventus', 'psg', 'ajax', 'porto', 'milan', 'roma', 'napoli',
                  'dortmund', 'lyon', 'marseille', 'atletico', 'villarreal',
                  'york city', 'portsmouth', 'plymouth', 'coventry', 'bristol city',
                  'sheffield', 'norwich', 'ipswich', 'sunderland', 'middlesbrough'}

SPORT_DETECT_PROMPT = """\
Classify: is "{query}" a football (soccer) team, a rugby union team, or unclear?
Reply with exactly one word: football / rugby / unclear"""

def detect_sport(query):
    """Detect sport via heuristics then Claude Haiku if needed."""
    q = query.lower().strip()
    for hint in RUGBY_HINTS:
        if hint in q:
            print(f"Sport detect (heuristic): rugby")
            return 'rugby'
    for hint in FOOTBALL_HINTS:
        if hint in q:
            print(f"Sport detect (heuristic): football")
            return 'football'
    # Haiku fallback
    try:
        import anthropic as _ant
        cl = _ant.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
        resp = cl.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=5,
            messages=[{'role': 'user', 'content': SPORT_DETECT_PROMPT.format(query=query)}]
        )
        result = (resp.content[0].text or '').strip().lower().split()[0]
        result = result if result in ('football', 'rugby') else 'unknown'
        print(f"Sport detect (Haiku): {result}")
        return result
    except Exception as e:
        print(f"Sport detect error: {e}")
        return 'unknown'

# ── Main orchestrator ─────────────────────────────────────────────────────────

def get_match_data(team_name, sport=None):
    """
    Full pipeline with fallbacks at each step.
    Returns match dict compatible with fmt_team(), or None if APIs return nothing.
    None triggers Claude web search fallback in main.py.
    """
    if not sport or sport == 'unknown':
        sport = detect_sport(team_name)

    if sport == 'unknown':
        print(f"Cannot determine sport for: {team_name}")
        return None

    print(f"get_match_data: team={team_name}, sport={sport}")

    # Step 1: Get team ID
    if sport == 'football':
        team_id = get_football_team_id(team_name)
    else:
        team_id = get_rugby_team_id(team_name)

    if not team_id:
        print(f"No team ID found for: {team_name}")
        return None

    # Step 2: Get fixture
    if sport == 'football':
        match = get_football_fixture(team_id, team_name)
    else:
        match = get_rugby_fixture(team_id, team_name)

    if not match:
        print(f"No fixture found for: {team_name} (id={team_id})")
        return None

    print(f"Fixture: {match['home_team']} vs {match['away_team']} ({match['next_date']})")

    # Ensure searched team appears first
    if name_matches(team_name, match.get('away_team', '')) and \
       not name_matches(team_name, match.get('home_team', '')):
        match['home_team'], match['away_team'] = match['away_team'], match['home_team']
        if match.get('venue'):
            match['venue'] = f"Away at {match['venue']}"

    # Step 3: Get odds
    odds = fetch_odds(match['home_team'], match['away_team'], sport)
    if odds:
        match.update(odds)
    else:
        match.update({'home_odds': '', 'draw_odds': '', 'away_odds': '',
                      'bookmaker': '', 'bookmaker_url': ''})

    # Step 4: TV channel via scrape (only useful for today's matches)
    tv = None
    if match.get('playing_today'):
        tv = scrape_tv_channel(team_name, sport)
    match['tv_channel']     = tv or ''
    match['radio_station']  = ''
    match['coverage_start'] = ''

    # Step 5: Fox fact via Claude Haiku (no web search)
    match['fox_fact'] = generate_fox_fact(match)

    return match
