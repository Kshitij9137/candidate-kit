"""Generates ~100k attendance logs and 500 employees for explain testing.
Indexes are created by app/main.py on startup; do NOT create them here."""
import os, random
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

# Wipe documents only — keeps the indexes that app/main.py created at startup.
db.employees.delete_many({})
db.attendance_logs.delete_many({})

# ---- 500 employees ------------------------------------------------------- #
employees = []
for i in range(1, 501):
    employees.append({
        "emp_code": f"EMP{i:04d}",
        "name": f"Employee {i}",
        "email": f"e{i}@x.com",
        "department": random.choice(["Engineering", "Sales", "Support", "HR", "Finance"]),
        "shift_start": random.choice(["09:30", "10:00", "22:00"]),
        "shift_end":   random.choice(["18:30", "19:00", "06:00"]),
        "joined_on": (datetime(2025, 1, 1) + timedelta(days=random.randint(0, 500))).date().isoformat(),
        "created_at": datetime.now(timezone.utc),
    })
db.employees.insert_many(employees)

# ---- ~100k attendance logs ---------------------------------------------- #
logs = []
base = datetime(2026, 1, 1, tzinfo=timezone.utc)
for e in employees:
    for d in range(200):
        if random.random() < 0.75:
            continue
        day = base + timedelta(days=d)
        status = random.choices(
            ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"],
            weights=[70, 10, 5, 10, 5],
        )[0]
        if status in ("ABSENT", "LEAVE"):
            pi = po = None
            wh = None
            lm = om = 0
            hd = False
        else:
            pi = day.replace(hour=4, minute=random.randint(0, 59), second=0, microsecond=0)
            po = pi + timedelta(hours=random.randint(4, 11), minutes=random.randint(0, 59))
            wh = round((po - pi).total_seconds() / 3600, 2)
            lm = random.choice([0, 0, 0, 0, 5, 15, 30, 45])
            om = random.choice([0, 0, 0, 0, 30, 60, 90])
            hd = wh < 4.5
        logs.append({
            "emp_code": e["emp_code"], "date": day.date().isoformat(),
            "status": status, "punch_in": pi, "punch_out": po,
            "work_hours": wh, "late_minutes": lm, "overtime_minutes": om,
            "half_day": hd, "history": [],
        })

for i in range(0, len(logs), 10_000):
    db.attendance_logs.insert_many(logs[i:i+10_000])

print("employees:", db.employees.count_documents({}))
print("attendance_logs:", db.attendance_logs.count_documents({}))