import datetime
import json
import os
import threading
import time

import pytz
import requests
from bs4 import BeautifulSoup
from espn_api.football import League
from espn_api.requests.espn_requests import ESPNAccessDenied
from google.cloud import bigquery


TABLES = {
    'leagues': 'commander.leagues',
    'projections': 'commander.projections',
    'scores': 'commander.scores',
}


def initialize_bigquery_client():
    """ Initialize BQ client with local or implied credentials """

    if not os.path.exists('google.json'):
        return bigquery.Client()

    credentials = service_account.Credentials.from_service_account_file(
        'google.json', scopes=["https://www.googleapis.com/auth/cloud-platform"])

    return bigquery.Client(credentials=credentials)


def load_profiles() -> dict:

    profiles = {}
    bq = bigquery.Client()

    for league in [league for league in bq.query(f"SELECT * FROM `{TABLES.get('leagues')}`").result()]:

        if league.profile not in profiles.keys():
            profiles[league.profile] = []

        profiles[league.profile].append({
            'name': league.name,
            'platform': league.platform,
            'scoring': league.scoring,
            'league_id': league.league_id,
            'team_id': league.team_id,
            'start_year': league.start_year,
            'swid': league.swid,
            's2': league.s2,
        })
    
    return profiles


def initialize_espn_league(league_id: int, year: int) -> League:

    s2 = swid = None
    profiles = load_profiles()

    for profile, leagues in profiles.items():
        for league in leagues:
            if league.get('league_id') == league_id:
                s2, swid = league.get('s2'), league.get('swid')

    return League(league_id=league_id, year=year, espn_s2=s2, swid=swid)


def get_current_week():
    season_start = datetime.datetime(2023, 9, 5, tzinfo=pytz.timezone("America/Chicago"))
    delta = get_current_central_datetime() - season_start
    return int(delta.days / 7) + 1


def get_current_central_datetime() -> datetime.datetime:
    return datetime.datetime.now(pytz.timezone('America/Chicago'))


def player_sort(item: dict) -> tuple:
    sorting_order = {'QB': 1, 'RB': 2, 'WR': 3, 'TE': 4, 'DST': 5, 'K': 6, 'BE': 10, 'IR': 11}
    try:
        return sorting_order.get(item.get('position'), 2)
    except TypeError as e:
        return 0


def translate_team(input: str, output: str, team_name: str) -> str:

    teams = [
        {'espn': 'WSH', 'sleeper': 'WAS', 'fp': 'WAS'},
        {'espn': 'JAX', 'sleeper': 'JAX', 'fp': 'JAC'},
        {'espn': 'OAK', 'sleeper': 'LV', 'fp': 'LV'},
    ]

    for team in teams:
        if team.get(input) == team_name:
            return team.get(output)
    
    return team_name


def get_all_projections(week: int) -> dict:

    runtime = datetime.datetime.utcnow()

    projections = {}

    for position_name in ['qb', 'rb', 'wr', 'te', 'k', 'dst']:
        for scoring in ['half-point-ppr', 'ppr']:

            if position_name in ['qb', 'k', 'dst']:
                url = f"https://www.fantasypros.com/nfl/rankings/{position_name}.php?week={week}"
            else:
                url = f"https://www.fantasypros.com/nfl/rankings/{scoring}-{position_name}.php?week={week}"

            for line in BeautifulSoup(requests.get(url).text, 'html.parser').find_all('script'):
                if 'ecrData' in line.text:

                    data = json.loads(line.text.split('\n')[5].split('var ecrData = ')[1].replace(';', ''))

                    for player in data.get('players'):

                        if position_name != 'dst':
                            name = ' '.join(player.get('player_name').split(' ')[0:2])
                        else:
                            name = f"{player.get('player_name').split(' ')[-1]} D/ST"
                        team = player.get('player_team_id')
                        position = player.get('player_position_id')
                        projected = player.get('r2p_pts')

                        if not projected:
                            continue

                        if team not in projections.keys():
                            projections[team] = {}
                        
                        if position not in projections.get(team).keys():
                            projections[team][position] = {}

                        if name not in projections.get(team).get(position).keys():
                            projections[team][position][name] = {}

                        projections[team][position][name][scoring] = float(projected)

    return projections


def get_league_data(data: dict, league: dict):

    data[league.get('name')] = []
    threads = []

    if league.get('platform') == 'espn':

        for year in range(league.get('start'), datetime.datetime.utcnow().year + 1):

            thread = threading.Thread(target=get_league_year_data, args=(data, year, league))
            thread.start()
            threads.append(thread)

    for thread in threads:
        thread.join()


def get_league_year_data(data: dict, year: int, league: dict):

    threads = []
    season = None

    while not season:
        try:
            season = initialize_espn_league(league.get('id'), year)
        except ESPNAccessDenied:
            time.sleep(0.5)


    week = 1

    for week in range(1, 15):
        
        thread = threading.Thread(target=get_league_week_data, args=(data, year, week, season, league))
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()


def get_league_week_data(data: dict, year: int, week: int, season: League, league: dict):

    threads = []
    matchup_data = None

    while not matchup_data:
        try:
            matchup_data = season.box_scores(week)
        except ESPNAccessDenied:
            time.sleep(0.5)
    
    if not matchup_data or matchup_data[0].is_playoff:
        return
        
    if year >= datetime.datetime.utcnow().year and week >= get_current_week():
        return

    matchup_id = 0

    for matchup in matchup_data:

        matchup_id += 1

        for team in (
            (matchup.home_team, matchup.home_score, matchup.home_projected),
            (matchup.away_team, matchup.away_score, matchup.away_projected)
        ):

            if team[1] == 0:
                continue

            owner = "Redacted" if team[0].owner == "None" else team[0].owner

            data[league.get('name')].append(
                (year, week, matchup_id, owner, round(team[1], 2), round(team[2], 2), round(team[1] - team[2], 2))
            )
