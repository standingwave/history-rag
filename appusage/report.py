#!/usr/bin/env python3
"""Print app-usage totals. Today plus the previous N days (default 7).

    python appusage/report.py         # today + last 7 days
    python appusage/report.py 30      # today + last 30 days
"""
import os, sys, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from appusage import store

def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    if not os.path.exists(store.APPUSAGE_DB):
        print(f"No data yet at {store.APPUSAGE_DB}. Is the daemon running?")
        return
    db = store.connect()
    store.setup(db)
    daily = store.daily_durations(db)
    today = datetime.date.today()

    for i in range(days + 1):
        day = (today - datetime.timedelta(days=i)).isoformat()
        apps = daily.get(day)
        if not apps:
            continue
        label = "today" if i == 0 else day
        total = sum(apps.values())
        print(f"\n{label}  ({store.fmt_duration(total)} tracked)")
        for app, secs in sorted(apps.items(), key=lambda kv: -kv[1]):
            print(f"  {store.fmt_duration(secs):>8}  {app}")

if __name__ == "__main__":
    main()
