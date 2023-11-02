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
    'changes': 'commander.changes',
}

app = Flask(__name__)


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
        if old.get('ppr') != new.get('ppr'):
            if abs(old.get('ppr', 0) - new.get('ppr', 0)) > 5:
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
    helpers.update_all_scores()
    return Response('Success', 200)


@app.route("/changes", methods=['GET'])
def list_changes():

    changes = []

    for change in helpers.run_query(f"SELECT * FROM `{TABLE_NAMES.get('changes')}` ORDER BY updated DESC LIMIT 20", as_list=True):
        change = dict(change)
        change['diff'] = f"<span class='change-{'negative' if change.get('old') > change.get('new') else 'positive'}'>" \
                         f"{'-' if change.get('old') > change.get('new') else '+'}{abs(change.get('old') - change.get('new'))}</span>"
        changes.append(change)

    return render_template('changes.html', changes=changes)


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
