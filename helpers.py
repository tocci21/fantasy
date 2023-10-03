import datetime
import json
import time
import threading
import os

import requests
from bs4 import BeautifulSoup
from espn_api.football import League
from espn_api.requests.espn_requests import ESPNAccessDenied

from constants import CDT


def initialize_espn_league(league_id: int, year: int) -> League:
    return League(league_id=league_id, year=year, espn_s2=os.environ.get('s2').replace(' ', ''), swid=os.environ.get('swid'))


def get_current_week():
    season_start = datetime.datetime(2023, 9, 5, tzinfo=CDT)
    delta = get_current_central_datetime() - season_start
    return int(delta.days / 7) + 1


def get_current_central_datetime() -> datetime.datetime:

    now = datetime.datetime.utcnow().replace(tzinfo=CDT)

    now += datetime.timedelta(hours=-5)

    if now.month >= 11 and now.day >= 5:
        now += datetime.timedelta(hours=-1)
    
    return now


def player_sort(item: dict) -> tuple:
    sorting_order = {'QB': 1, 'DST': 3, 'D/ST': 3, 'K': 4, 'BE': 5, 'IR': 6}
    try:
        return (sorting_order.get(item.get('position'), 2), -item.get('points'), -item.get('projected'))
    except TypeError as e:
        return (0, 0, 0, 0)


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


def get_all_projections() -> dict:

    runtime = datetime.datetime.utcnow()

    projections = {}

    for position_name in ['qb', 'rb', 'wr', 'te', 'k', 'dst']:
        for scoring in ['half-point-ppr', 'ppr']:
        
            if position_name in ['qb', 'k', 'dst']:
                url = f"https://www.fantasypros.com/nfl/rankings/{position_name}.php"
            else:
                url = f"https://www.fantasypros.com/nfl/rankings/{scoring}-{position_name}.php"

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


def get_league_data(data: dict, league: tuple):

    data[league[0]] = []
    threads = []

    if league[1] == 'espn':

        for year in range(league[3], datetime.datetime.utcnow().year + 1):

            thread = threading.Thread(target=get_league_year_data, args=(data, year, league))
            thread.start()
            threads.append(thread)

    for thread in threads:
        thread.join()


def get_league_year_data(data: dict, year: int, league: tuple):

    threads = []
    season = None

    while not season:
        try:
            season = initialize_espn_league(league[2], year)
        except ESPNAccessDenied:
            time.sleep(0.5)


    week = 1

    for week in range(1, 15):
        
        thread = threading.Thread(target=get_league_week_data, args=(data, year, week, season, league))
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()


def get_league_week_data(data: dict, year: int, week: int, season: League, league: tuple):

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

            data[league[0]].append(
                (year, week, matchup_id, owner, round(team[1], 2), round(team[2], 2), round(team[1] - team[2], 2))
            )
