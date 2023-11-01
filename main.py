import datetime
import threading

import pytz
import requests
from flask import Flask, render_template, request, Response
from google.cloud import bigquery

import helpers

TABLE_NAMES = {
    'teams': 'commander.teams',
    'projections': 'commander.projections',
    'scores': 'commander.scores',
}

app = Flask(__name__)


def lookup_projected(projections: dict, name: str, team: str, position: str, scoring: str) -> float:
    if position in ['D/ST', 'DEF']:
        position = 'DST'
    try:
        return projections.get(team, 0).get(position, 0).get(name, 0).get(scoring, 0)
    except (AttributeError, KeyError):
        return 0


# def get_current_points(data: dict, platform: str, league_id: int, scoring: str, week: int) -> list:


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


@app.route("/update/all", methods=['GET'])
def update_all():
    """ Update all but live scores """

    responses = []

    responses.append(('projections', update_projections()))
    responses.append(('teams', update_teams()))

    response = ', '.join(f"{key}: {value}" for key, value in responses)

    return Response(response, status=200 if False not in [r[1] for r in responses] else 500)


def update_projections(week: int = helpers.get_current_week()):

    runtime = helpers.get_current_central_datetime().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    responses = []
    
    table = helpers.TABLES.get('projections')

    projections = helpers.get_all_projections(week)

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

    schema = [
        {"name": "player",          "type": "STRING",   "mode": "REQUIRED"},
        {"name": "team",            "type": "STRING",   "mode": "REQUIRED"},
        {"name": "week",            "type": "INTEGER",  "mode": "REQUIRED"},
        {"name": "standard",        "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "half-point-ppr",  "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "ppr",             "type": "FLOAT",    "mode": "REQUIRED"},
        {"name": "updated",         "type": "DATETIME", "mode": "REQUIRED"},
    ]

    helpers.write_to_bigquery(table, schema, rows)
    helpers.run_query(f"DELETE FROM `{table}` WHERE week = {week} AND updated < '{runtime}'")

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

    mode = request.args.get('mode') if 'mode' in request.args.keys() else 'default'
    week = int(request.args.get('week')) if 'week' in request.args.keys() else helpers.get_current_week()

    matchups = helpers.get_all_matchups(profile, week, mode)

    return render_template('leagues.html', matchups=matchups, week=week)


if __name__ == '__main__':
    app.run()
