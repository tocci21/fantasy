import datetime
import json
import os

import requests
from bs4 import BeautifulSoup
from espn_api.football import League
from flask import Flask, render_template
from google.cloud import secretmanager_v1
from yaml import safe_load

CDT = datetime.timezone(datetime.timedelta(hours=-5))
PRO_MATCHUPS = {}

app = Flask(__name__)


def load_profiles() -> dict:

    client = secretmanager_v1.SecretManagerServiceClient()
    name = f"projects/{os.environ.get('project')}/secrets/fantasy-profiles/versions/latest"
    data = client.access_secret_version(name=name).payload.data.decode("UTF-8")

    return  json.loads(data)


def initialize_league(platform: str, league_id: int, year: int):

    if platform == 'espn':
        return League(league_id=league_id, year=year, espn_s2=os.environ.get('s2').replace(' ', ''), swid=os.environ.get('swid'))


def sleeper_gametime_lookup(team: str,) -> tuple:
    translations = {'WAS': 'WSH', 'LV': 'OAK'}
    return PRO_MATCHUPS.get(translations.get(team) if translations.get(team) else team)


def get_current_week():
    season_start = datetime.datetime(2023, 9, 5, tzinfo=CDT)
    delta = get_current_central_datetime() - season_start
    return int(delta.days / 7) + 1


def get_current_points(platform: str, league_id: int, week: int) -> list:

    matchups = []

    if platform == 'espn':

        league = initialize_league(platform, league_id, 2023)

        for game in league.box_scores(week):

            matchup = []

            for team_data, team_roster in ((game.home_team, game.home_lineup), (game.away_team, game.away_lineup)):

                players = [{'id': team_data.team_id, 'name': team_data.owner.split(' ')[0].strip()}]

                for player_data in team_roster:

                    player = {
                        'name': player_data.name,
                        'status': player_data.injuryStatus,
                        'points': player_data.points,
                        'projected': player_data.projected_points,
                        'position': player_data.position,
                        'slot': player_data.slot_position,
                        'gametime': player_data.game_date.replace(tzinfo=CDT),
                    }

                    if get_current_central_datetime() >= player.get('gametime'):
                        if player_data.game_played == 100:
                            player['play_status'] = 'played'
                        else:
                            player['play_status'] = 'playing'
                    else:
                        player['play_status'] = 'future'

                    players.append(player)

                    if player_data.proTeam not in PRO_MATCHUPS.keys():
                        PRO_MATCHUPS[player_data.proTeam] = (player_data.game_date, player_data.game_played == 100)

                matchup.append(players)
            matchups.append(matchup)
    
    if platform == 'sleeper':

        all_players = requests.get('https://api.sleeper.app/v1/players/nfl').json()
        rosters = requests.get(f'https://api.sleeper.app/v1/league/{league_id}/rosters').json()
        users = requests.get(f'https://api.sleeper.app/v1/league/{league_id}/users').json()

        count = 0

        matchup = []

        for team in sorted(
            requests.get(f'https://api.sleeper.app/v1/league/{league_id}/matchups/{week}').json(),
            key=lambda x: x.get('matchup_id')):

            team_info = {}

            for roster in rosters:
                if roster.get('roster_id') == team.get('roster_id'):
                    for user in users:
                        if roster.get('owner_id') == user.get('user_id'):
                            team_info = {'id': team.get('roster_id'), 'name': user.get('display_name')}
                            break
                    if team_info:
                        break

            players = [team_info]

            for i in range(0, len(team.get('starters'))):

                player_data = all_players.get(team.get('starters')[i])

                if not player_data:
                    continue

                player = {
                    'id': team.get('starters')[i],
                    'name': player_data.get('full_name', f"{player_data.get('last_name')} D/ST"),
                    'status': player_data.get('injury_status', ''),
                    'points': team.get('starters_points')[i],
                    'projected': -1,
                    'position': player_data.get('fantasy_positions')[0],
                    'slot': player_data.get('fantasy_positions')[0],
                }

                gametime, gamedone = sleeper_gametime_lookup(player_data.get('team'))

                player['gametime'] = gametime

                if get_current_central_datetime() >= gametime.replace(tzinfo=CDT):
                    if gamedone:
                        player['play_status'] = 'played'
                    else:
                        player['play_status'] = 'playing'
                else:
                    player['play_status'] = 'future'

                if player.get('position') == 'DEF':
                    player['position'] = 'D/ST'
                    player['slot'] = 'D/ST'

                players.append(player)
            
            matchup.append(players)

            count += 1

            if not count % 2:
                matchups.append(matchup)
                matchup = []

    return matchups


def player_sort(item: dict) -> int:
    sorting_order = {'QB': 1, 'D/ST': 3, 'K': 4, 'BE': 5, 'IR': 6}
    return (sorting_order.get(item.get('position'), 2), -item.get('points'), item.get('gametime'), -item.get('projected'))


def get_current_central_datetime() -> datetime.datetime:

    now = datetime.datetime.now(CDT)

    if now.month >= 11 and now.day >= 5:
        now += datetime.timedelta(hours=-1)
    
    return now


def organize_team(players: list) -> dict:

    team = {'info': players[0], 'active': [], 'inactive': [], 'points': 0}

    for player in players[1:]:

        if player.get('play_status') != 'future':
            player['display'] = player.get('points')
        else:
            player['display'] = player.get('gametime').strftime("%a %I:%M")  # .replace('Sun', 'S').replace('Mon', 'M').replace('Thu', 'T')

        team['inactive' if player.get('slot') in ['BE', 'IR'] else 'active'].append(player)

    for key in ['active', 'inactive']:
        team[key] = sorted(team.get(key), key=player_sort)

    for player in team.get('active'):
        team['points'] += player.get('points')

    return team


@app.route("/<string:profile>", methods=['GET'])
def index_profile(profile: str):
    return index_week(profile, get_current_week())


@app.route("/<string:profile>/<int:week>", methods=['GET'])
def index_week(profile: str, week: int):
    
    profile = PROFILES.get(profile)

    if not profile:
        return f"Profile not found. Profiles: {', '.join(PROFILES.keys())}"

    matchup_db = {}
    team_db = {}
    matchups = []

    for league_data in profile:

        name = league_data[0]
        platform = league_data[1]
        league_id = int(league_data[2])
        team_id = int(league_data[3])

        matchup_db[league_id] = {'name': name, 'points': get_current_points(platform, league_id, week)}
        team_db[league_id] = team_id

    for league_id, league_matchups in matchup_db.items():
        for matchup in league_matchups.get('points'):
            if matchup[0][0].get('id') == team_db.get(league_id):
                matchups.append((league_matchups.get('name'), organize_team(matchup[0]), organize_team(matchup[1])))
                break
            if matchup[1][0].get('id') == team_db.get(league_id):
                matchups.append((league_matchups.get('name'), organize_team(matchup[1]), organize_team(matchup[0])))
                break

    return render_template('leagues.html', matchups=matchups)


@app.route("/", methods=['GET'])
def index():
    return ""


if __name__ == '__main__':

    PROFILES = load_profiles()
    app.run()
