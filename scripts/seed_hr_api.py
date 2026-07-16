#!/usr/bin/env python3
"""
Seed ~3 months of dummy HR data (Employees, Attendance, Leave) into a live
Frappe HR site via its REST API. Idempotent: safe to re-run, skips records
that already exist.

Flow: fetch companies -> create employees -> create attendance ("hours")
-> create leave allocations + applications.

Auth (env vars, required):
    FRAPPE_URL          e.g. https://hr.yourdomain.com
    FRAPPE_API_KEY
    FRAPPE_API_SECRET

Optional tuning (env vars):
    EMPLOYEES_PER_COMPANY   default 5
    MONTHS_BACK             default 3
    WORKING_HOURS_MIN       default 7.5
    WORKING_HOURS_MAX       default 9.0

Run:
    FRAPPE_URL=... FRAPPE_API_KEY=... FRAPPE_API_SECRET=... python3 seed_hr_api.py

Or drop the same vars into scripts/.env (see scripts/.env.example) and just run:
    python3 seed_hr_api.py
"""

import json
import os
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

FRAPPE_URL = os.environ.get("FRAPPE_URL", "").rstrip("/")
API_KEY = os.environ.get("FRAPPE_API_KEY", "")
API_SECRET = os.environ.get("FRAPPE_API_SECRET", "")

EMPLOYEES_PER_COMPANY = int(os.environ.get("EMPLOYEES_PER_COMPANY", 5))
MONTHS_BACK = int(os.environ.get("MONTHS_BACK", 3))
WORKING_HOURS_MIN = float(os.environ.get("WORKING_HOURS_MIN", 7.5))
WORKING_HOURS_MAX = float(os.environ.get("WORKING_HOURS_MAX", 9.0))

LEAVE_TYPES = ["Casual Leave", "Sick Leave"]

if not (FRAPPE_URL and API_KEY and API_SECRET):
    sys.exit("Set FRAPPE_URL, FRAPPE_API_KEY, FRAPPE_API_SECRET env vars first.")

session = requests.Session()
session.headers.update(
    {
        "Authorization": f"token {API_KEY}:{API_SECRET}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
)


def _request(method, path, **kwargs):
    url = f"{FRAPPE_URL}{path}"
    for attempt in range(3):
        try:
            resp = session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code >= 500 and attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
        return resp
    raise RuntimeError(f"unreachable: {url}")


def get_list(doctype, filters=None, fields=None, limit=0):
    params = {"limit_page_length": limit}
    if fields:
        params["fields"] = json.dumps(fields)
    if filters:
        params["filters"] = json.dumps(filters)
    resp = _request("GET", f"/api/resource/{doctype}", params=params)
    resp.raise_for_status()
    return resp.json()["data"]


def exists(doctype, filters):
    return bool(get_list(doctype, filters=filters, fields=["name"], limit=1))


def create(doctype, data):
    """POST a new doc. If data includes docstatus=1, Frappe submits it
    inline (fires on_submit / ledger entries) in the same call."""
    resp = _request("POST", f"/api/resource/{doctype}", json=data)
    if resp.status_code >= 400:
        print(f"  [FAIL] {doctype}: {resp.status_code} {resp.text[:300]}")
        return None
    return resp.json()["data"]


def business_days(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            yield d
        d += timedelta(days=1)


def ensure_leave_types():
    for lt in LEAVE_TYPES:
        if not exists("Leave Type", [["name", "=", lt]]):
            print(f"Creating Leave Type: {lt}")
            create("Leave Type", {"leave_type_name": lt, "is_earned_leave": 0})


def ensure_holiday_list(company, today):
    """Leave Application validation requires a Holiday List assigned to the
    company (or employee); without one, every Leave Application 417s."""
    if exists(
        "Holiday List Assignment",
        [["applicable_for", "=", "Company"], ["assigned_to", "=", company], ["docstatus", "=", 1]],
    ):
        return

    year_start = date(today.year, 1, 1)
    year_end = date(today.year, 12, 31)
    list_name = f"{company} Dummy Seed {today.year}"

    if not exists("Holiday List", [["name", "=", list_name]]):
        print(f"  Creating Holiday List: {list_name}")
        create("Holiday List", {"holiday_list_name": list_name, "from_date": str(year_start), "to_date": str(year_end)})

    print(f"  Assigning Holiday List to {company}")
    create(
        "Holiday List Assignment",
        {
            "applicable_for": "Company",
            "assigned_to": company,
            "holiday_list": list_name,
            "from_date": str(year_start),
            "docstatus": 1,
        },
    )


def seed_employee(company, index, today, start_date):
    slug = f"{company.lower().replace(' ', '_')}_emp_{index}"
    email = f"{slug}@example.com"

    if exists("Employee", [["company_email", "=", email]]):
        emp_name = get_list("Employee", filters=[["company_email", "=", email]], fields=["name"])[0]["name"]
        print(f"  Employee {email} already exists ({emp_name})")
    else:
        payload = {
            "first_name": f"Operator{index}",
            "last_name": company,
            "company": company,
            "status": "Active",
            "gender": "Male" if index % 2 == 0 else "Female",
            "date_of_birth": "1994-05-12",
            "date_of_joining": str(start_date - timedelta(days=30)),
            "company_email": email,
            "personal_email": email,
        }
        doc = create("Employee", payload)
        if not doc:
            print(
                "  -> Employee creation failed, likely a missing mandatory field on "
                "your site (e.g. department/designation). Check the error above, "
                "add the field to `payload`, and re-run."
            )
            return
        emp_name = doc["name"]
        print(f"  Created Employee {emp_name} ({email})")

    seed_leave(emp_name, company, today, start_date)
    seed_attendance(emp_name, company, today, start_date)


def seed_leave(emp_name, company, today, start_date):
    year_start = date(today.year, 1, 1)
    year_end = date(today.year, 12, 31)

    for lt in LEAVE_TYPES:
        if exists(
            "Leave Allocation",
            [["employee", "=", emp_name], ["leave_type", "=", lt], ["from_date", "=", str(year_start)]],
        ):
            continue
        create(
            "Leave Allocation",
            {
                "employee": emp_name,
                "leave_type": lt,
                "from_date": str(year_start),
                "to_date": str(year_end),
                "new_leaves_allocated": 15.0,
                "company": company,  # required; not auto-fetched via API
                "docstatus": 1,  # submit inline so ledger entries are created
            },
        )

    leave_dates = set()
    offset = 0
    while offset < (today - start_date).days:
        leave_date = start_date + timedelta(days=offset + random.randint(0, 3))
        if leave_date > today:
            break
        if not exists("Leave Application", [["employee", "=", emp_name], ["from_date", "=", str(leave_date)]]):
            create(
                "Leave Application",
                {
                    "employee": emp_name,
                    "leave_type": random.choice(LEAVE_TYPES),
                    "from_date": str(leave_date),
                    "to_date": str(leave_date),
                    "half_day": 0,
                    "status": "Approved",
                    "posting_date": str(leave_date),
                    "company": company,
                    "description": "Seeded dummy data",
                    "docstatus": 1,
                },
            )
            leave_dates.add(leave_date)
        offset += 15
    return leave_dates


def seed_attendance(emp_name, company, today, start_date):
    for d in business_days(start_date, today):
        if exists("Attendance", [["employee", "=", emp_name], ["attendance_date", "=", str(d)]]):
            continue
        create(
            "Attendance",
            {
                "employee": emp_name,
                "attendance_date": str(d),
                "company": company,
                "status": "Present",
                "working_hours": round(random.uniform(WORKING_HOURS_MIN, WORKING_HOURS_MAX), 2),
            },
        )


def main():
    today = date.today()
    start_date = today - timedelta(days=MONTHS_BACK * 30)

    companies = [c["name"] for c in get_list("Company", fields=["name"])]
    if not companies:
        sys.exit("No companies found on this site. Create at least one Company first.")
    print(f"Found companies: {companies}")

    ensure_leave_types()

    for company in companies:
        print(f"\nSeeding company: {company}")
        ensure_holiday_list(company, today)
        for i in range(1, EMPLOYEES_PER_COMPANY + 1):
            seed_employee(company, i, today, start_date)

    print("\nDone.")


if __name__ == "__main__":
    main()
