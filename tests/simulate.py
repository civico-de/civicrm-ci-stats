"""Synthetic Duderino rows with realistic spread, host load and PR mix."""
import random
from datetime import datetime, timedelta

SUITES = {"phpunit-api3-1": 1275, "phpunit-api3-2": 1432, "phpunit-crm-1": 715, "phpunit-crm-2": 932, "phpunit-crm-3": 1296,
          "phpunit-crm-4": 909, "phpunit-api4": 691, "phpunit-core-exts": 422, "phpunit-civi": 185, "mixin karma": 370}
NODES = ["test-3", "test-4", "test-5"]
START = datetime(2026, 9, 1)


def jobs(seed, days=30, per_day=16, effect=None, spread=0.09):
    """effect(suite, node, moment, pr) returns a duration multiplier."""
    rng = random.Random(seed)
    node_day = {(n, d): rng.gauss(1, 0.04) for n in NODES for d in range(days)}
    rows, batch = [], 1000
    for day in range(days):
        for number in range(per_day):
            moment = START + timedelta(days=day, seconds=rng.randint(0, 86399))
            batch += 1
            pr = str(rng.randint(30000, 30000 + per_day * days // 2))
            for suite, base in SUITES.items():
                node = rng.choice(NODES)
                diurnal = 1 + 0.05 * (8 <= moment.hour <= 20)
                value = base * rng.lognormvariate(0, spread) * node_day[node, day] * diurnal
                value *= effect(suite, node, moment, pr) if effect else 1
                rows.append({
                    "batch_type": "CiviCRM-PR", "batch_id": str(batch), "task_id": str(batch * 100), "civiver": "master", "pr": pr,
                    "bldtype": "standalone-clean", "bkprof": "php85m57", "suite": suite, "ornery": "disable", "outcome": "SUCCESS",
                    "node": node, "image": "x.img", "started": moment.strftime("%Y-%m-%d %H:%M:%S"),
                    "completed": (moment + timedelta(seconds=value + 120)).strftime("%Y-%m-%d %H:%M:%S"),
                    "duration": f"{value:.1f}", "wall": str(int(value + 120)), "passed": "2000", "failed": "0", "skipped": "10",
                })
    return rows
