import datetime
import json
import requests

from constants import CDT, PROFILES, NO_GAMETIME
from helpers import initialize_espn_league, translate_team, player_sort, get_current_central_datetime, get_current_week, get_all_projections

from flask import Flask, render_template
from google.cloud import secretmanager_v1
from yaml import safe_load


app = Flask(__name__)


def lookup_projected(projections: dict, name: str, team: str, position: str, scoring: str) -> float:

    position = position.replace('/', '').replace('DEF', 'DST')

    if not projections or not name or not team or not position or not scoring:
        return 0

    try:
        return projections.get(team).get(position).get(name).get(scoring)
    except AttributeError:
        print(f"NOT FOUND: Name: {name} | Team: {team} | Position: {position}")
        return 0


def get_current_points(data: dict, platform: str, league_id: int, scoring: str, week: int) -> list:

    matchups = []

    if platform == 'espn':

        league = initialize_espn_league(platform, league_id, 2023)

        for game in league.box_scores(week):

            matchup = []

            for team_data, team_roster in ((game.home_team, game.home_lineup), (game.away_team, game.away_lineup)):

                players = [{'id': team_data.team_id, 'name': team_data.owner.split(' ')[0].strip()}]

                for player_data in team_roster:

                    player = {
                        'name': player_data.name,
                        'status': player_data.injuryStatus,
                        'points': player_data.points,
                        'projected': lookup_projected(
                            data.get('projections'),
                            ' '.join(player_data.name.split(' ')[0:2]),
                            translate_team('espn', 'fp', player_data.proTeam),
                            player_data.position,
                            scoring
                        ),
                        'position': player_data.position,
                        'slot': player_data.slot_position.replace('/', ''),
                    }

                    if player.get('projected') == 0 and player.get('status') == 'active':
                        player['status'] = 'warning'

                    try:
                        player['gametime'] = player_data.game_date.replace(tzinfo=CDT) + datetime.timedelta(hours=-5)
                    except AttributeError as e:
                        player['gametime'] = NO_GAMETIME

                    if 'D/ST' not in player.get('name'):
                        player['name'] = f"{player.get('name')[0]}. {player.get('name').split(' ')[1]}"

                    if get_current_central_datetime() >= player.get('gametime'):
                        player['play_status'] = 'played' if player_data.game_played == 100 else 'playing'

                    else:
                        player['play_status'] = 'future'

                    players.append(player)

                    if player.get('gametime') != NO_GAMETIME:
                        data['pro_matchups'][player_data.proTeam] = (player.get('gametime'), player_data.game_played == 100)

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
                    'projected': lookup_projected(
                        data.get('projections'),
                        ' '.join(player_data.get('full_name', f"{player_data.get('last_name')} D/ST").split(' ')[0:2]),
                        translate_team('sleeper', 'fp', player_data.get('team')),
                        player_data.get('fantasy_positions')[0],
                        scoring
                    ),
                    'position': player_data.get('fantasy_positions')[0],
                    'slot': player_data.get('fantasy_positions')[0],
                }

                if player.get('projected') == 0 and player.get('status') == None:
                    player['status'] = 'warning'

                if 'D/ST' not in player.get('name'):
                    player['name'] = f"{player.get('name')[0]}. {' '.join(player.get('name').split(' ')[1:])}"

                gametime, gamedone = data.get('pro_matchups').get(translate_team('sleeper', 'espn', player_data.get('team')))

                player['gametime'] = gametime
                player['play_status'] = 'future' if get_current_central_datetime() < gametime else 'played' if gamedone else 'playing'

                if player.get('position') == 'DEF':
                    player['position'] = player['slot'] = 'DST'

                players.append(player)
            
            matchup.append(players)

            count += 1

            if not count % 2:
                matchups.append(matchup)
                matchup = []

    return matchups


def organize_team(players: list) -> dict:

    team = {'info': players[0], 'active': [], 'inactive': [], 'points': 0, 'projected': 0}

    for player in players[1:]:

        if player.get('play_status') != 'future':
            player['display_even'] = player['display_odd'] = player.get('points')
        elif player.get('gametime') != NO_GAMETIME:
            player['display_even'] = player.get('gametime') \
            .strftime("%a %I:%M").replace('Sun', 'S').replace('Mon', 'M').replace('Thu', 'T')
            player['display_odd'] = ' '.join(reversed(player.get('display_even').split(' ')))
        else:
            player['display_even'] = player['display_odd'] = 'N/A'

        team['inactive' if player.get('slot') in ['BE', 'IR'] else 'active'].append(player)

    for key in ['active', 'inactive']:
        team[key] = sorted(team.get(key), key=player_sort)

    for player in team.get('active'):
        team['points'] += player.get('points')
        team['projected'] += player.get('projected')
    
    team['projected'] = round(team.get('projected'), 2)

    return team


@app.route("/", methods=['GET'])
def index():
    return ""


@app.route("/<string:profile>", methods=['GET'])
def index_profile(profile: str):
    return index_week(profile, get_current_week())


@app.route("/<string:profile>/<int:week>", methods=['GET'])
def index_week(profile: str, week: int):

    profile = PROFILES.get(profile)

    data = {'projections': get_all_projections(), 'pro_matchups': {}}

    if not profile:
        return f"Profile not found. Profiles: {', '.join(PROFILES.keys())}"

    matchup_db = {}
    team_db = {}
    matchups = []

    for league_data in profile:

        name = league_data[0]
        platform = league_data[1]
        scoring = league_data[2]
        league_id = int(league_data[3])
        team_id = int(league_data[4])

        matchup_db[league_id] = {'name': name, 'points': get_current_points(data, platform, league_id, scoring, week)}
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


if __name__ == '__main__':
    app.run()
