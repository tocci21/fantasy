import datetime


CDT = datetime.timezone(datetime.timedelta(hours=-5))

NO_GAMETIME = datetime.datetime(2000, 1, 1, tzinfo=CDT)

PROFILES = {
    'david': [
        ['Z League', 'espn', 'half-point-ppr', 30191259, 6],
        ['D League', 'espn', 'ppr', 1447623889, 1],
        ['T League', 'sleeper', 'ppr', 992213857386684416, 8]
    ],
    'marisol': [
        ['Z League', 'espn', 'half-point-ppr', 30191259, 3],
        ['D League', 'espn', 'ppr', 1447623889, 9],
        ['T League', 'sleeper', 'ppr', 992213857386684416, 12]
    ]
}
