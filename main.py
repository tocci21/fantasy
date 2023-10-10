import datetime
import json
import threading

import pytz
import requests
from flask import Flask, render_template, request
from google.cloud import secretmanager_v1
from yaml import safe_load

import helpers

NO_GAMETIME = datetime.datetime(2000, 1, 1, tzinfo=pytz.timezone("America/Chicago"))

app = Flask(__name__)


def lookup_projected(projections: dict, name: str, team: str, position: str, scoring: str) -> float:
    if position in ['D/ST', 'DEF']:
        position = 'DST'
    try:
        return projections.get(team, 0).get(position, 0).get(name, 0).get(scoring, 0)
    except (AttributeError, KeyError):
        return 0


def get_current_points(data: dict, platform: str, league_id: int, scoring: str, week: int) -> list:

    matchups = []

    if platform == 'espn':

        league = helpers.initialize_espn_league(league_id, 2023)

        for game in league.box_scores(week):

            matchup = []

            for team_data, team_roster in ((game.home_team, game.home_lineup), (game.away_team, game.away_lineup)):

                players = [{'id': team_data.team_id, 'name': team_data.owner.split(' ')[0].strip(), 'platform': 'espn'}]

                for player_data in team_roster:

                    player = {
                        'name': player_data.name,
                        'status': player_data.injuryStatus,
                        'points': player_data.points,
                        'projected': lookup_projected(
                            data.get('projections'),
                            ' '.join(player_data.name.split(' ')[0:2]),
                            helpers.translate_team('espn', 'fp', player_data.proTeam),
                            player_data.position,
                            scoring
                        ),
                        'position': player_data.position.replace('/', ''),
                        'slot': player_data.slot_position.replace('/', ''),
                    }

                    if player.get('projected') == 0 and player.get('status') in ['active', 'normal']:
                        player['status'] = 'warning'

                    try:
                        player['gametime'] = player_data.game_date.astimezone(pytz.timezone('America/Chicago'))
                    except AttributeError as e:
                        player['gametime'] = NO_GAMETIME

                    if 'D/ST' not in player.get('name'):
                        player['name'] = f"{player.get('name')[0]}. {player.get('name').split(' ')[1]}"

                    if helpers.get_current_central_datetime() >= player.get('gametime'):
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
                            team_info = {'id': team.get('roster_id'), 'name': user.get('display_name'), 'platform': 'sleeper'}
                            break
                    if team_info:
                        break

            players = [team_info]

            for i in team.get('players'):

                player_data = all_players.get(i)

                if not player_data:
                    continue

                player = {
                    'id': i,
                    'name': player_data.get('full_name', f"{player_data.get('last_name')} D/ST"),
                    'status': player_data.get('injury_status', ''),
                    'points': team.get('players_points').get(i),
                    'projected': lookup_projected(
                        data.get('projections'),
                        ' '.join(player_data.get('full_name', f"{player_data.get('last_name')} D/ST").split(' ')[0:2]),
                        helpers.translate_team('sleeper', 'fp', player_data.get('team')),
                        player_data.get('fantasy_positions')[0],
                        scoring
                    ),
                    'position': player_data.get('fantasy_positions')[0].replace('DEF', 'DST'),
                    'slot': player_data.get('fantasy_positions')[0].replace('DEF', 'DST') if i in team.get('starters') else 'BE',
                }

                if player.get('projected') == 0 and player.get('status') == None:
                    player['status'] = 'warning'

                if 'D/ST' not in player.get('name'):
                    player['name'] = f"{player.get('name')[0]}. {' '.join(player.get('name').split(' ')[1:])}"

                gametime, gamedone = data.get('pro_matchups').get(helpers.translate_team('sleeper', 'espn', player_data.get('team')), (None, None))

                player['gametime'] = gametime
                player['play_status'] = 'future' if not gametime or helpers.get_current_central_datetime() < gametime else 'played' if gamedone else 'playing'

                if player.get('position') == 'DEF':
                    player['position'] = player['slot'] = 'DST'

                players.append(player)
            
            matchup.append(players)

            count += 1

            if not count % 2:
                matchups.append(matchup)
                matchup = []

    return matchups


def organize_team(players: list, mode: str = 'default') -> dict:

    team = {'info': players[0], 'roster': [], 'active': [], 'inactive': [], 'points': 0, 'projected': 0}

    for player in players[1:]:

        if player.get('play_status') != 'future':
            player['display_even'] = player['display_odd'] = player.get('points')
        elif player.get('gametime') != NO_GAMETIME:
            if not player.get('gametime'):
                player['display_even'] = 'n/a'
            else:
                player['display_even'] = player.get('gametime') \
                .strftime("%a %I:%M").replace('Sun', 'S').replace('Mon', 'M').replace('Thu', 'T')
            player['display_odd'] = ' '.join(reversed(player.get('display_even').split(' ')))
        else:
            player['display_even'] = player['display_odd'] = 'N/A'

        team['inactive' if player.get('slot') in ['BE', 'IR'] else 'active'].append(player)

    for key in ['active', 'inactive']:
        team[key] = sorted(team.get(key), key=helpers.player_sort)

    ordered_players = sorted(players[1:], key=lambda x: x.get('projected', 0), reverse=True)

    if mode == 'max':

        for position in ['QB', 'RB', 'WR', 'FLEX', 'TE', 'DST', 'K']:
            if position != 'FLEX':
                team['roster'].append((position, [p for p in ordered_players if p.get('position') == position][0]))

            else:
                for p in ordered_players:
                    if p.get('position') in ['RB', 'WR', 'TE'] and p not in [op[1] for op in team.get('roster')]:
                        team['roster'].append((position, p))
                        break

            if position in ['RB', 'WR']:
                team['roster'].append((position, [p for p in ordered_players if p.get('position') == position][1]))

            if team.get('info').get('platform') == 'sleeper' and position == 'FLEX':
                for p in ordered_players:
                    if p.get('position') in ['RB', 'WR', 'TE'] and p not in [op[1] for op in team.get('roster')]:
                        team['roster'].append((position, p))
                        break
        
        team['roster'] = [p[1] for p in team.get('roster')]
    
    elif mode == 'default':

        team['roster'].extend(team['active'])
    
    elif mode == 'all':
    
        for key in ['active', 'inactive']:
            team['roster'].extend(team[key])

    for player in team.get('roster'):
        team['points'] += player.get('points')
        team['projected'] += player.get('projected')

    team['projected'] = round(team.get('projected'), 2)

    return team


@app.route("/records", methods=['GET'])
def records():

    leagues = []
    records = {}
    data = {}

    profiles = helpers.load_profiles()

    for league_list in profiles.values():
        
        for league in league_list:

            league_data = {
                'name': league.get('name'),
                'id': league.get('league_id'),
                'platform': league.get('platform'),
                'start': league.get('start_year')
            }
            
            if league_data not in leagues:
                leagues.append(league_data)

    threads = []

    for league in leagues:

        thread = threading.Thread(target=helpers.get_league_data, args=(data, league))
        thread.start()
        threads.append(thread)
    
    for thread in threads:
        thread.join()

    for league_name, league_data in data.items():

        if not league_data:
            continue

        records[league_name] = {
            'Highest Points (Week)': sorted(league_data, key=lambda x: x[4], reverse=True)[0:3],
            'Lowest Points (Week)': sorted(league_data, key=lambda x: x[4])[0:3],
            'Highest Projected (Week)': sorted(league_data, key=lambda x: x[5], reverse=True)[0:3],
            'Lowest Projected (Week)': sorted(league_data, key=lambda x: x[5])[0:3],
            'Best Outcome (Week)': sorted(league_data, key=lambda x: x[6], reverse=True)[0:3],
            'Worst Outcome (Week)': sorted(league_data, key=lambda x: x[6])[0:3],
        }

    return render_template('records.html', records=records)


@app.route("/", methods=['GET'])
def index():
    return ""


@app.route("/<string:profile>/", methods=['GET'])
def index_week(profile: str):

    profiles = helpers.load_profiles()
    profile = profiles.get(profile)

    week = request.args.get('week') if 'week' in request.args.keys() else helpers.get_current_week()
    mode = request.args.get('mode') if 'mode' in request.args.keys() else 'default'

    data = {'projections': helpers.get_all_projections(), 'pro_matchups': {}}

    if not profile:
        return f"Profile not found. Profiles: {', '.join(profiles.keys())}"

    matchup_db = {}
    team_db = {}
    matchups = []

    for league in profile:

        matchup_db[league.get('league_id')] = {
            'name': league.get('name'),
            'points': get_current_points(data, league.get('platform'), league.get('league_id'), league.get('scoring'), week)
        }

        team_db[league.get('league_id')] = league.get('team_id')

    for league_id, league_matchups in matchup_db.items():
        for matchup in league_matchups.get('points'):
            if matchup[0][0].get('id') == team_db.get(league_id):
                matchups.append((league_matchups.get('name'), organize_team(matchup[0], mode), organize_team(matchup[1], mode)))
                break
            if matchup[1][0].get('id') == team_db.get(league_id):
                matchups.append((league_matchups.get('name'), organize_team(matchup[1], mode), organize_team(matchup[0], mode)))
                break

    return render_template('leagues.html', matchups=matchups)


if __name__ == '__main__':
    app.run()
