# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, attendance regularization, listing, and analytics.

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Environment Variables

| Variable | Meaning | Example |
|---|---|---|
| `MONGO_URI` | MongoDB connection string (local or Atlas `mongodb+srv://`) | `mongodb://localhost:27017` |
| `MONGO_DB` | Target database name | `hrone_attendance` |

*Note:* Process environment variables win over values in local `.env` (`load_dotenv(override=False)`). Do not commit `.env`.

MongoDB **6.0+** is required (`$densify`, `$setWindowFields`). Indexes are created idempotently at application startup.

## Run

```bash
python sample_seed.py          # seed 6 employees & 9 attendance logs
uvicorn app.main:app --port 8000
```

Health check: `GET /health` → `200` if MongoDB pings, else `503`.

## Implemented

- **Employees:** Create (`POST /employees`), list with department filter and pagination (`GET /employees`).
- **Attendance Writes:**
  - Punch-in (`POST /attendance/punch-in`) with IST shift normalization (R1), lateness calculation (R2), and unique index concurrency safety.
  - Punch-out (`POST /attendance/punch-out`) across midnight, work hours (R4), overtime thresholding (R3), half-day calculation (R5), and atomic double-punch prevention.
  - Regularization (`PATCH /attendance/{emp_code}/{date}`) with audit history tracking, parameter validation, and optimistic concurrency control.
- **Attendance Listing:** Database-side compound index sorting (`date DESC, emp_code ASC`) and pagination (`GET /attendance`).
- **Analytics:**
  - Employee monthly (`GET /analytics/employees/{emp_code}/monthly`) with mid-month joiner working days and half-up rounding.
  - Department summary (`GET /analytics/departments/summary`) preserving zero-log employee headcount.
  - Late leaderboard (`GET /analytics/leaderboard/late`) with competition ranking (1,2,2,4) via `$setWindowFields`.
  - Department trend (`GET /analytics/departments/{department}/trend`) returning exactly one row per calendar day via `$densify` with 7-day moving averages.
- **Admin Query Diagnostics:** `GET /admin/explain/{endpoint}` returning `executionStats` and winning index plans.

## Missing / Limitations

- **Authentication / Authorization:** No JWT/API-key auth layer is included (per assignment scope and OpenAPI specification).
- **Materialized Views:** Analytics pipelines execute aggregate queries on demand; at 100x scale (>10M logs), nightly rollups or pre-aggregated summary collections would be recommended.
