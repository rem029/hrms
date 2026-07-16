#!/usr/bin/env python3
"""
Seed ~3 months of dummy HR data (Employees, Departments, Designations,
Attendance, Leave) into a live Frappe HR site via its REST API. Idempotent:
safe to re-run, skips records that already exist.

Flow: fetch companies -> seed departments/designations -> create employees
-> create attendance ("hours") -> create leave allocations + applications.

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
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from faker import Faker

load_dotenv(Path(__file__).resolve().parent / ".env")

FRAPPE_URL = os.environ.get("FRAPPE_URL", "").rstrip("/")
API_KEY = os.environ.get("FRAPPE_API_KEY", "")
API_SECRET = os.environ.get("FRAPPE_API_SECRET", "")

EMPLOYEES_PER_COMPANY = int(os.environ.get("EMPLOYEES_PER_COMPANY", 5))
MONTHS_BACK = int(os.environ.get("MONTHS_BACK", 3))
WORKING_HOURS_MIN = float(os.environ.get("WORKING_HOURS_MIN", 7.5))
WORKING_HOURS_MAX = float(os.environ.get("WORKING_HOURS_MAX", 9.0))

LEAVE_TYPES = ["Casual Leave", "Sick Leave"]
DEPARTMENTS = ["Engineering", "Sales", "Marketing", "Human Resources", "Finance", "Operations"]
DESIGNATIONS = [
    "Software Engineer",
    "Sales Executive",
    "Marketing Specialist",
    "HR Executive",
    "Accountant",
    "Operations Manager",
    "Product Manager",
    "Business Analyst",
]

if not (FRAPPE_URL and API_KEY and API_SECRET):
    sys.exit("Set FRAPPE_URL, FRAPPE_API_KEY, FRAPPE_API_SECRET env vars first.")

fake = Faker()

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


# Exceptions Frappe raises for "this already exists" or "this is expected to
# eventually stop working" cases (e.g. leave balance runs out after enough
# repeated runs). Treat these as a quiet skip, not a failure.
DUPLICATE_EXC_TYPES = {
    "DuplicateAttendanceError",
    "OverlapError",
    "DuplicateEntryError",
    "BackDatedAllocationError",
    "AttendanceAlreadyMarkedError",
    "InsufficientLeaveBalanceError",
    "OverlappingShiftAttendanceError",
}


def create(doctype, data, quiet_duplicates=False, log=True):
    """POST a new doc. If data includes docstatus=1, Frappe submits it
    inline (fires on_submit / ledger entries) in the same call."""
    resp = _request("POST", f"/api/resource/{doctype}", json=data)
    if resp.status_code >= 400:
        exc_type = None
        try:
            exc_type = resp.json().get("exc_type")
        except ValueError:
            pass
        if quiet_duplicates and exc_type in DUPLICATE_EXC_TYPES:
            print(f"  (skip) {doctype} already exists")
            return None
        print(f"  [FAIL] {doctype}: {resp.status_code} {resp.text[:300]}")
        return None
    result = resp.json()["data"]
    if log:
        print(f"  + {doctype}: {result['name']}")
    return result


def random_employee_id(max_attempts=20):
    """Employee's naming_series-based autoname silently discards any client-
    supplied `name` on insert (Frappe wipes it in set_new_name() unless the
    doctype's naming rule is 'Set by User'), so a random ID has to be applied
    as a rename *after* creation instead."""
    for _ in range(max_attempts):
        candidate = f"HR-EMP-{random.randint(10000, 99999)}"
        if not exists("Employee", [["name", "=", candidate]]):
            return candidate
    raise RuntimeError("Could not find a free HR-EMP-<5 digits> id after 20 attempts")


def rename_doc(doctype, old_name, new_name):
    resp = _request(
        "POST", "/api/method/frappe.client.rename_doc", json={"doctype": doctype, "old_name": old_name, "new_name": new_name}
    )
    if resp.status_code >= 400:
        print(f"  [FAIL] rename {doctype} {old_name} -> {new_name}: {resp.status_code} {resp.text[:300]}")
        return old_name
    return resp.json().get("message", new_name)


def business_days(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            yield d
        d += timedelta(days=1)


def ensure_leave_types():
    for lt in LEAVE_TYPES:
        if not exists("Leave Type", [["name", "=", lt]]):
            create("Leave Type", {"leave_type_name": lt, "is_earned_leave": 0})


FIXED_HOLIDAYS = [
    ((1, 1), "New Year's Day"),
    ((5, 1), "Labour Day"),
    ((12, 25), "Christmas Day"),
]


def build_holiday_rows(year_start, year_end):
    """Weekly offs (Sat/Sun, matching business_days()'s Mon-Fri definition)
    plus a few fixed-date holidays -- enough that the calendar isn't empty,
    without pretending to model a real country's official holiday list."""
    rows = []
    seen = set()

    def add(d, desc, weekly_off=False):
        if year_start <= d <= year_end and d not in seen:
            seen.add(d)
            rows.append({"holiday_date": str(d), "description": desc, "weekly_off": 1 if weekly_off else 0})

    for (month, day), desc in FIXED_HOLIDAYS:
        add(date(year_start.year, month, day), desc)

    d = year_start
    while d <= year_end:
        if d.weekday() >= 5:  # Sat/Sun
            add(d, "Weekend", weekly_off=True)
        d += timedelta(days=1)

    return rows


def backfill_holiday_rows(list_name, year_start, year_end):
    """Older runs created Holiday List shells with zero actual holidays in
    them; fill those in now regardless of how the list was created."""
    resp = _request("GET", f"/api/resource/Holiday List/{list_name}")
    if resp.status_code >= 400:
        return
    if not resp.json()["data"].get("holidays"):
        print(f"  Backfilling holidays into: {list_name}")
        _request("PUT", f"/api/resource/Holiday List/{list_name}", json={"holidays": build_holiday_rows(year_start, year_end)})


def ensure_holiday_list(company, today):
    """Leave Application validation requires a Holiday List assigned to the
    company (or employee); without one, every Leave Application 417s."""
    year_start = date(today.year, 1, 1)
    year_end = date(today.year, 12, 31)
    list_name = f"{company} Dummy Seed {today.year}"

    assignment = get_list(
        "Holiday List Assignment",
        filters=[["applicable_for", "=", "Company"], ["assigned_to", "=", company], ["docstatus", "=", 1]],
        fields=["holiday_list"],
        limit=1,
    )
    if assignment:
        # Look up whatever list is *actually* assigned rather than assuming
        # it matches the naming convention -- it may not (e.g. an earlier,
        # differently-named list from before this convention existed).
        backfill_holiday_rows(assignment[0]["holiday_list"], year_start, year_end)
        return

    if not exists("Holiday List", [["name", "=", list_name]]):
        create(
            "Holiday List",
            {
                "holiday_list_name": list_name,
                "from_date": str(year_start),
                "to_date": str(year_end),
                "holidays": build_holiday_rows(year_start, year_end),
            },
        )

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


def ensure_departments(company):
    """Department names are only unique per company, and Frappe controls its
    own autoname (e.g. 'Engineering - DO'), so we look up the real name
    rather than assume it."""
    names = []
    for dept in DEPARTMENTS:
        existing = get_list(
            "Department", filters=[["department_name", "=", dept], ["company", "=", company]], fields=["name"], limit=1
        )
        if existing:
            names.append(existing[0]["name"])
            continue
        doc = create("Department", {"department_name": dept, "company": company})
        if doc:
            names.append(doc["name"])
    return names


def ensure_designations():
    """Designation is global (not company-scoped) and autonames directly
    from designation_name, so the name is known without a lookup."""
    for desig in DESIGNATIONS:
        if not exists("Designation", [["name", "=", desig]]):
            create("Designation", {"designation_name": desig})
    return DESIGNATIONS


SHIFT_NAME = "Day Shift"
SHIFT_START = clock_time(9, 0)
SHIFT_END = clock_time(18, 0)
LATE_GRACE_MINUTES = 15
EARLY_EXIT_GRACE_MINUTES = 15


def ensure_shift_type():
    if exists("Shift Type", [["name", "=", SHIFT_NAME]]):
        return SHIFT_NAME
    create(
        "Shift Type",
        {
            "name": SHIFT_NAME,  # Shift Type's naming rule is "Set by User"
            "start_time": SHIFT_START.strftime("%H:%M:%S"),
            "end_time": SHIFT_END.strftime("%H:%M:%S"),
            "enable_late_entry_marking": 1,
            "late_entry_grace_period": LATE_GRACE_MINUTES,
            "enable_early_exit_marking": 1,
            "early_exit_grace_period": EARLY_EXIT_GRACE_MINUTES,
        },
    )
    return SHIFT_NAME


def ensure_shift_assignment(emp_name, company, start_date):
    if exists("Shift Assignment", [["employee", "=", emp_name], ["shift_type", "=", SHIFT_NAME]]):
        return
    create(
        "Shift Assignment",
        {
            "employee": emp_name,
            "company": company,
            "shift_type": SHIFT_NAME,
            "start_date": str(start_date),
            "status": "Active",
            "docstatus": 1,
        },
        quiet_duplicates=True,
    )


def unique_personal_email(max_attempts=10):
    """Faker emails aren't guaranteed unique across separate script runs
    (Faker's own .unique tracking resets per process), so check live data
    and regenerate on collision instead of trusting randomness alone."""
    for _ in range(max_attempts):
        candidate = fake.email()
        if not exists("Employee", [["personal_email", "=", candidate]]):
            return candidate
    return fake.email()


def seed_employee(company, index, today, start_date, departments, designations):
    slug = f"{company.lower().replace(' ', '_')}_emp_{index}"
    email = f"{slug}@example.com"

    if exists("Employee", [["company_email", "=", email]]):
        existing = get_list(
            "Employee",
            filters=[["company_email", "=", email]],
            fields=["name", "first_name", "last_name", "personal_email"],
        )[0]
        emp_name = existing["name"]
        print(
            f"  Employee slot '{email}' already seeded -> {emp_name} "
            f"({existing['first_name']} {existing['last_name']}, {existing['personal_email']})"
        )
    else:
        gender = random.choice(["Male", "Female"])
        first_name = fake.first_name_male() if gender == "Male" else fake.first_name_female()
        payload = {
            "first_name": first_name,
            "last_name": fake.last_name(),
            "company": company,
            "status": "Active",
            "gender": gender,
            "date_of_birth": str(fake.date_of_birth(minimum_age=22, maximum_age=58)),
            "date_of_joining": str(start_date - timedelta(days=30)),
            "company_email": email,
            "personal_email": unique_personal_email(),
            "department": random.choice(departments) if departments else None,
            "designation": random.choice(designations) if designations else None,
        }
        doc = create("Employee", payload, log=False)  # a nicer custom line follows below
        if not doc:
            print(
                "  -> Employee creation failed, likely a missing mandatory field on "
                "your site. Check the error above, add the field to `payload`, and re-run."
            )
            return
        emp_name = rename_doc("Employee", doc["name"], random_employee_id())
        print(
            f"  Created Employee {emp_name} ({first_name} {payload['last_name']}, "
            f"{payload['personal_email']}) [slot: {email}]"
        )

    ensure_shift_assignment(emp_name, company, start_date)
    leave_dates = seed_leave(emp_name, company, today, start_date)
    seed_attendance(emp_name, company, today, start_date, leave_dates)
    seed_checkins(emp_name, start_date, today)  # backfill for any older rows created before shift tracking existed
    seed_payroll(emp_name, company, today)


_company_currency_cache = {}


def get_company_currency(company):
    if company not in _company_currency_cache:
        rows = get_list("Company", filters=[["name", "=", company]], fields=["default_currency"], limit=1)
        _company_currency_cache[company] = rows[0]["default_currency"] if rows else "USD"
    return _company_currency_cache[company]


def last_full_month(today):
    first_of_this_month = today.replace(day=1)
    month_end = first_of_this_month - timedelta(days=1)
    return month_end.replace(day=1), month_end


def ensure_salary_structure(company, currency):
    name = f"Standard - {company}"
    if exists("Salary Structure", [["name", "=", name]]):
        return name
    create(
        "Salary Structure",
        {
            "name": name,  # Salary Structure's naming rule is "Set by User" -- a name is required
            "company": company,
            "currency": currency,
            "is_active": "Yes",
            "payroll_frequency": "Monthly",
            "earnings": [{"salary_component": "Basic", "amount_based_on_formula": 1, "formula": "base"}],
            "deductions": [{"salary_component": "Income Tax", "amount_based_on_formula": 1, "formula": "base * 0.05"}],
            "docstatus": 1,
        },
    )
    return name


def seed_payroll(emp_name, company, today):
    currency = get_company_currency(company)
    structure_name = ensure_salary_structure(company, currency)
    month_start, month_end = last_full_month(today)

    doj_rows = get_list("Employee", filters=[["name", "=", emp_name]], fields=["date_of_joining"], limit=1)
    date_of_joining = date.fromisoformat(doj_rows[0]["date_of_joining"]) if doj_rows else month_start
    if date_of_joining > month_end:
        return  # wasn't employed yet during the last full month -- nothing to pay them for

    # Assignment from_date can't be before the employee's joining date.
    assignment_from = max(month_start, date_of_joining)

    if not exists("Salary Structure Assignment", [["employee", "=", emp_name]]):
        create(
            "Salary Structure Assignment",
            {
                "employee": emp_name,
                "salary_structure": structure_name,
                "company": company,
                "currency": currency,
                "from_date": str(assignment_from),
                "base": round(random.uniform(6000, 15000), 2),
                "docstatus": 1,
            },
            quiet_duplicates=True,
        )

    # Slip period can't start before the assignment takes effect either.
    slip_start = assignment_from

    if exists("Salary Slip", [["employee", "=", emp_name], ["start_date", "=", str(slip_start)]]):
        return
    doc = create(
        "Salary Slip",
        {
            "employee": emp_name,
            "company": company,
            "posting_date": str(month_end),
            "salary_structure": structure_name,
            "start_date": str(slip_start),
            "end_date": str(month_end),
            "currency": currency,
        },
        quiet_duplicates=True,
    )
    if doc:
        # Salary Slip names contain "/" (e.g. "Sal Slip/HR-EMP-.../00001"),
        # which must be percent-encoded or the path segment breaks routing.
        _request("PUT", f"/api/resource/Salary Slip/{quote(doc['name'], safe='')}", json={"docstatus": 1})


def seed_checkins(emp_name, start_date, today):
    """IN/OUT Employee Checkin logs backing each Present/WFH Attendance day,
    so attendance looks like it came from a clock-in system rather than
    being typed in by hand."""
    attendance_rows = get_list(
        "Attendance",
        filters=[
            ["employee", "=", emp_name],
            ["attendance_date", "between", [str(start_date), str(today)]],
            ["status", "in", ["Present", "Work From Home"]],
            ["docstatus", "=", 1],
        ],
        fields=["name", "attendance_date", "working_hours"],
    )
    for att in attendance_rows:
        att_date = date.fromisoformat(att["attendance_date"])
        day_start, day_end = f"{att_date} 00:00:00", f"{att_date} 23:59:59"
        if exists("Employee Checkin", [["employee", "=", emp_name], ["time", "between", [day_start, day_end]]]):
            continue
        check_in = datetime.combine(att_date, clock_time(random.randint(8, 9), random.randint(0, 59)))
        hours = att.get("working_hours") or round(random.uniform(WORKING_HOURS_MIN, WORKING_HOURS_MAX), 2)
        check_out = check_in + timedelta(hours=hours)
        for log_type, ts in (("IN", check_in), ("OUT", check_out)):
            create(
                "Employee Checkin",
                {
                    "employee": emp_name,
                    "log_type": log_type,
                    "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    # Not linking `attendance` here: Frappe's validate_time_change()
                    # throws "cannot modify time" if attendance+time are both set
                    # on insert -- it doesn't distinguish new record from edit.
                    "skip_auto_attendance": 1,  # Attendance already created separately; don't let a background job double up
                },
                quiet_duplicates=True,
            )


ATTENDANCE_STATUS_WEIGHTS = [("Present", 0.85), ("Absent", 0.08), ("Work From Home", 0.07)]
LEAVE_OUTCOME_WEIGHTS = [("Approved", 0.8), ("Open", 0.12), ("Rejected", 0.08)]
LEAVE_APPROVER = "elawrenceponce@gmail.com"  # explicit user choice -- will receive real notification emails


def existing_leave_dates(emp_name, start_date, today):
    rows = get_list(
        "Leave Application",
        filters=[["employee", "=", emp_name], ["from_date", "between", [str(start_date), str(today)]]],
        fields=["from_date"],
    )
    return {date.fromisoformat(r["from_date"]) for r in rows}


def cancel_conflicting_attendance(emp_name, leave_date):
    """A Leave Application can't submit for a date Attendance already marked
    Present/WFH on (AttendanceAlreadyMarkedError) -- cancel that record so
    the leave can go through instead of just avoiding the date forever."""
    conflicts = get_list(
        "Attendance",
        filters=[
            ["employee", "=", emp_name],
            ["attendance_date", "=", str(leave_date)],
            ["status", "in", ["Present", "Work From Home"]],
            ["docstatus", "=", 1],
        ],
        fields=["name"],
    )
    for c in conflicts:
        _request("PUT", f"/api/resource/Attendance/{c['name']}", json={"docstatus": 2})


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

    # Freshly randomized every run (not seeded) so re-running actually adds
    # new leave days instead of proposing the same dates forever. Dates
    # already used for leave are skipped; anything else that collides with
    # an existing Attendance record gets that record cancelled first.
    already = existing_leave_dates(emp_name, start_date, today)
    all_leave_dates = set(already)
    offset = 0
    while offset < (today - start_date).days:
        leave_date = start_date + timedelta(days=offset + random.randint(0, 3))
        offset += 15
        if leave_date > today or leave_date in already:
            continue

        outcome = random.choices(
            [o for o, _ in LEAVE_OUTCOME_WEIGHTS], weights=[w for _, w in LEAVE_OUTCOME_WEIGHTS]
        )[0]

        # validate_attendance() blocks any status (even a draft/pending one)
        # from saving on a date Attendance already marked Present/WFH on.
        cancel_conflicting_attendance(emp_name, leave_date)

        payload = {
            "employee": emp_name,
            "leave_type": random.choice(LEAVE_TYPES),
            "from_date": str(leave_date),
            "to_date": str(leave_date),
            "half_day": 0,
            "status": outcome,
            "posting_date": str(leave_date),
            "company": company,
            "description": fake.sentence(),
            "leave_approver": LEAVE_APPROVER,
        }
        if outcome != "Open":
            payload["docstatus"] = 1  # Open stays a draft (pending); submit rejects on_submit otherwise

        doc = create("Leave Application", payload, quiet_duplicates=True)
        if not doc:
            continue

        if outcome == "Rejected":
            # Attendance was cancelled to let the (rejected) request through;
            # the employee was actually there that day, so restore it.
            create(
                "Attendance",
                {
                    "employee": emp_name,
                    "attendance_date": str(leave_date),
                    "company": company,
                    "status": "Present",
                    "working_hours": round(random.uniform(WORKING_HOURS_MIN, WORKING_HOURS_MAX), 2),
                    "docstatus": 1,
                },
                quiet_duplicates=True,
            )
        else:
            all_leave_dates.add(leave_date)

    return all_leave_dates


def generate_shift_times(d):
    """~20% chance of a late arrival (past the grace period), ~10% chance of
    an early exit -- everything else lands within the shift's grace window."""
    if random.random() < 0.2:
        in_offset = LATE_GRACE_MINUTES + random.randint(1, 30)
    else:
        in_offset = random.randint(-15, LATE_GRACE_MINUTES - 1)
    check_in = datetime.combine(d, SHIFT_START) + timedelta(minutes=in_offset)

    if random.random() < 0.1:
        out_offset = -(EARLY_EXIT_GRACE_MINUTES + random.randint(1, 30))
    else:
        out_offset = random.randint(-(EARLY_EXIT_GRACE_MINUTES - 1), 60)
    check_out = datetime.combine(d, SHIFT_END) + timedelta(minutes=out_offset)

    late_entry = in_offset > LATE_GRACE_MINUTES
    early_exit = out_offset < -EARLY_EXIT_GRACE_MINUTES
    return check_in, check_out, late_entry, early_exit


def create_checkin_pair(emp_name, check_in, check_out):
    for log_type, ts in (("IN", check_in), ("OUT", check_out)):
        create(
            "Employee Checkin",
            {
                "employee": emp_name,
                "log_type": log_type,
                "shift": SHIFT_NAME,
                "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                # Not linking `attendance`: Frappe's validate_time_change() throws
                # "cannot modify time" if attendance+time are both set on insert.
                "skip_auto_attendance": 1,  # Attendance already created separately; don't let a background job double up
            },
            quiet_duplicates=True,
        )


def seed_attendance(emp_name, company, today, start_date, leave_dates):
    for d in business_days(start_date, today):
        if d in leave_dates:
            continue  # the Leave Application covers this date instead
        if exists("Attendance", [["employee", "=", emp_name], ["attendance_date", "=", str(d)]]):
            continue
        status = random.choices([s for s, _ in ATTENDANCE_STATUS_WEIGHTS], weights=[w for _, w in ATTENDANCE_STATUS_WEIGHTS])[0]

        payload = {
            "employee": emp_name,
            "attendance_date": str(d),
            "company": company,
            "status": status,
            "docstatus": 1,  # submit inline; don't rely on any site-side auto-submit
        }

        check_in = check_out = None
        if status == "Absent":
            payload["working_hours"] = 0
        else:
            check_in, check_out, late_entry, early_exit = generate_shift_times(d)
            payload["working_hours"] = round((check_out - check_in).total_seconds() / 3600, 2)
            payload["shift"] = SHIFT_NAME
            payload["late_entry"] = 1 if late_entry else 0
            payload["early_exit"] = 1 if early_exit else 0

        doc = create("Attendance", payload, quiet_duplicates=True)
        if doc and check_in:
            create_checkin_pair(emp_name, check_in, check_out)


def next_employee_index(company):
    """EMPLOYEES_PER_COMPANY new employees should be created on every run,
    not just once -- so start counting from whatever slot index is already
    the highest for this company instead of always starting back at 1
    (which would just keep re-hitting existing slots and skipping)."""
    prefix = f"{company.lower().replace(' ', '_')}_emp_"
    rows = get_list(
        "Employee", filters=[["company_email", "like", f"{prefix}%@example.com"]], fields=["company_email"]
    )
    max_index = 0
    for row in rows:
        suffix = row["company_email"][len(prefix) :].split("@")[0]
        if suffix.isdigit():
            max_index = max(max_index, int(suffix))
    return max_index + 1


def main():
    today = date.today()
    start_date = today - timedelta(days=MONTHS_BACK * 30)

    companies = [c["name"] for c in get_list("Company", fields=["name"])]
    if not companies:
        sys.exit("No companies found on this site. Create at least one Company first.")
    print(f"Found companies: {companies}")

    ensure_leave_types()
    ensure_shift_type()
    designations = ensure_designations()

    for company in companies:
        print(f"\nSeeding company: {company}")
        ensure_holiday_list(company, today)
        departments = ensure_departments(company)
        start_index = next_employee_index(company)
        for i in range(start_index, start_index + EMPLOYEES_PER_COMPANY):
            seed_employee(company, i, today, start_date, departments, designations)

    print("\nDone.")


if __name__ == "__main__":
    main()
