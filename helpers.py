import datetime
import json
import time
import os

import requests
from bs4 import BeautifulSoup
from espn_api.football import League

from constants import CDT


def initialize_espn_league(platform: str, league_id: int, year: int) -> League:

    if platform == 'espn':
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
        return (sorting_order.get(item.get('position'), 2), -item.get('points'), item.get('gametime'), -item.get('projected'))
    except TypeError as e:
        print(item)
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