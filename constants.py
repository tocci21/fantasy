import datetime


CDT = datetime.timezone(datetime.timedelta(hours=-5))

NO_GAMETIME = datetime.datetime(2000, 1, 1, tzinfo=CDT)

PROFILES = {
    'david': [
        ['Z League', 'espn', 'half-point-ppr', 30191259, 6, 2019],
        ['D League', 'espn', 'ppr', 1447623889, 1, 2022],
        ['T League', 'sleeper', 'ppr', 992213857386684416, 8, 2022]
    ],
    'marisol': [
        ['Z League', 'espn', 'half-point-ppr', 30191259, 3, 2019],
        ['D League', 'espn', 'ppr', 1447623889, 9, 2022],
        ['T League', 'sleeper', 'ppr', 992213857386684416, 12, 2022]
    ]
}
