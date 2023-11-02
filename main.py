import datetime
import json
import threading

import pytz
import requests
from flask import Flask, Response, render_template, request
from google.cloud import secretmanager_v1
from yaml import safe_load
from google.cloud import bigquery

import helpers

NO_GAMETIME = datetime.datetime(2000, 1, 1, tzinfo=pytz.timezone("America/Chicago"))
TABLE_NAMES = {
    'projections': 'commander.projections',
    'scores': 'commander.scores',
    'changes': 'commander.changes',
}

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

                players = [{'id': team_data.team_id, 'name': team_data.team_name, 'platform': 'espn'}]

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

                    if player.get('projected') == 0 and player.get('status') == 'ACTIVE':
                        player['status'] = 'warning'

                    try:
                        player['gametime'] = player_data.game_date.astimezone(pytz.timezone('America/Chicago'))
                    except AttributeError as e:
                        player['gametime'] = NO_GAMETIME

                    if 'D/ST' not in player.get('name'):
                        player['name'] = f"{player.get('name')[0]}. {player.get('name').split(' ')[1]}"

                    if player.get('gametime') == NO_GAMETIME:
                        player['play_status'] = 'future'
                    elif helpers.get_current_central_datetime() >= player.get('gametime'):
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
                player['display_even'] = 'BYE'
            else:
                player['display_even'] = player.get('gametime') \
                .strftime("%a %I:%M").replace('Sun', 'S').replace('Mon', 'M').replace('Thu', 'T')
            player['display_odd'] = ' '.join(reversed(player.get('display_even').split(' ')))
        else:
            player['display_even'] = player['display_odd'] = 'BYE'

        team['inactive' if player.get('slot') in ['BE', 'IR'] else 'active'].append(player)

    for key in ['active', 'inactive']:
        team[key] = sorted(team.get(key), key=helpers.player_sort)

    ordered_players = sorted(players[1:], key=lambda x: x.get('projected', 0), reverse=True)

    if mode == 'max':

        for position in ['QB', 'RB', 'WR', 'TE', 'FLEX', 'DST', 'K']:
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


@app.route("/update/projections", methods=['GET'])
def update_projections():

    runtime = helpers.get_current_central_datetime().strftime('%Y-%m-%d %H:%M:%S')
    week = helpers.get_current_week()
    rows = []
    changes = []
    responses = []

    old_projections = helpers.run_query(f"SELECT * FROM `{TABLE_NAMES.get('projections')}` WHERE week = {week}")
    projections = helpers.get_all_projections(week)

    projections_np = {}

    remove_positions = []

    for team, team_data in projections.items():
        projections_np[team] = {}
        for position, position_data in team_data.items():
            if position not in remove_positions:
                remove_positions.append(position)
            for player_name, player_data in position_data.items():
                projections_np[team][player_name] = player_data

    for player in old_projections:
        player = dict(player)
        old = {'half-point-ppr': player.get('half-point-ppr'), 'ppr': player.get('ppr')}
        new = projections_np.get(player.get('team'), {}).get(player.get('player'), {})
        if old != new:
            for key, value in old.items():
                if abs(old.get(key, 0) - new.get(key, 0)) > 5:
                    changes.append({
                        'player': player.get('player'),
                        'team': player.get('team'),
                        'scoring': key,
                        'old': old.get(key, 0),
                        'new': new.get(key, 0),
                        'updated': runtime,
                    })

    return
    for team, team_data in projections.items():
        for position in team_data.values():
            for player, scoring in position.items():
                row = {
                    'player': player,
                    'team': team,
                    'week': week,
                    'standard': 0,
                    'half-point-ppr': scoring.get('half-point-ppr', 0),
                    'ppr': scoring.get('ppr', 0),
                    'updated': runtime,
                }
                rows.append(row)

    bq = helpers.initialize_bigquery_client()

    schema = [
        {"name": "player",          "type": "STRING",   "mode": "REQUIRED"},
        {"name": "team",            "type": "STRING",   "mode": "REQUIRED"},
        {"name": "week",            "type": "INTEGER",  "mode": "REQUIRED"},
        {"name": "standard",        "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "half-point-ppr",  "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "ppr",             "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "updated",         "type": "DATETIME", "mode": "REQUIRED"},
    ]

    helpers.write_to_bigquery(helpers.TABLES.get('projections'), schema, rows)
    helpers.run_query(f"DELETE FROM `{table}` WHERE week = {week} AND updated < '{runtime}'")

    schema = [
        {"name": "player",          "type": "STRING",   "mode": "REQUIRED"},
        {"name": "team",            "type": "STRING",   "mode": "REQUIRED"},
        {"name": "scoring",         "type": "STRING",   "mode": "REQUIRED"},
        {"name": "old",             "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "new",             "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "updated",         "type": "DATETIME", "mode": "REQUIRED"},
    ]

    helpers.write_to_bigquery(helpers.TABLES.get('changes'), schema, rows)

    return True


def update_teams():

    leagues = []
    rows = []
    responses = []

    for profile in helpers.load_profiles().values():
        for league in profile:
            if league.get('league_id') not in [l.get('league_id') for l in leagues]:
                leagues.append(league)

    for league in leagues:

        if league.get('platform') == 'espn':

            url = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/2023/segments/0/leagues/{league.get('league_id')}?view=mTeam"

            data = requests.get(url, cookies={'espn_s2': league.get('s2'), 'swid': league.get('swid')}).json()

            owner_map = {}

            for member in data.get('members'):
                owner_map[member.get('id')] = f"{member.get('firstName')} {member.get('lastName')}"

            for team in data.get('teams'):
                rows.append({
                    'league_id': league.get('league_id'),
                    'team_id': team.get('id'),
                    'team': helpers.cleanup(team.get('name', 'None')),
                    'owner': helpers.cleanup(owner_map.get(team.get('owners', ['None'])[0], 'None')),
                })

        if league.get('platform') == 'sleeper':

            rosters = {}

            for roster in requests.get(f"https://api.sleeper.app/v1/league/{league.get('league_id')}/rosters").json():
                rosters[roster.get('owner_id')] = roster.get('roster_id')
            
            for user in requests.get(f"https://api.sleeper.app/v1/league/{league.get('league_id')}/users").json():
                if not rosters.get(user.get('user_id')):
                    continue
                rows.append({
                    'league_id': league.get('league_id'),
                    'team_id': rosters.get(user.get('user_id')),
                    'team': user.get('metadata').get('team_name') if user.get('metadata').get('team_name') else user.get('display_name'),
                    'owner': user.get('display_name'),
                })

        table = TABLE_NAMES.get('teams')

        schema = [
            {"name": "league_id", "type": "INTEGER", "mode": "REQUIRED"},
            {"name": "team_id",   "type": "INTEGER", "mode": "REQUIRED"},
            {"name": "team",      "type": "STRING",  "mode": "REQUIRED"},
            {"name": "owner",     "type": "STRING",  "mode": "REQUIRED"},
        ]

        for row in rows:
            print(row)

        helpers.run_query(f"TRUNCATE TABLE {table}")
        helpers.write_to_bigquery(table, schema, rows)

    return True


@app.route("/update/scores", methods=['GET'])
def update_scores():

    runtime = helpers.get_current_central_datetime().strftime('%Y-%m-%d %H:%M:%S')
    week = helpers.get_current_week()
    rows = []
    
    table = helpers.TABLES.get('scores')

    scores = helpers.get_all_scores(week)

    for team, team_data in projections.items():
        for position in team_data.values():
            for player, scoring in position.items():
                row = {
                    'player': player,
                    'team': team,
                    'week': week,
                    'standard': 0,
                    'half-point-ppr': scoring.get('half-point-ppr', 0),
                    'ppr': scoring.get('ppr', 0),
                    'updated': runtime,
                }
                rows.append(row)

    bq = helpers.initialize_bigquery_client()

    schema = [
        {"name": "player",          "type": "STRING",   "mode": "REQUIRED"},
        {"name": "team",            "type": "STRING",   "mode": "REQUIRED"},
        {"name": "week",            "type": "INTEGER",  "mode": "REQUIRED"},
        {"name": "standard",        "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "half-point-ppr",  "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "ppr",             "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "updated",         "type": "DATETIME", "mode": "REQUIRED"},
    ]

    job_config = bigquery.LoadJobConfig(schema=schema, source_format='NEWLINE_DELIMITED_JSON')
    job = bq.load_table_from_json(rows, table, job_config=job_config)
    job.result()

    bq.query(f"DELETE FROM `{table}` WHERE updated < '{runtime}'").result()


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

    week = int(request.args.get('week')) if 'week' in request.args.keys() else helpers.get_current_week()
    mode = request.args.get('mode') if 'mode' in request.args.keys() else 'default'

    data = {'projections': helpers.get_all_projections(week), 'pro_matchups': {}}

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
    
    return render_template('leagues.html', matchups=matchups, week=week)


if __name__ == '__main__':
    app.run()
