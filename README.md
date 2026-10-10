# Employee Attendance & Analytics API

[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg?logo=fastapi)](https://fastapi.tiangolo.com)
[![MongoDB](https://img.shields.io/badge/MongoDB-6.0%2B%20%2F%207.0-47A248.svg?logo=mongodb)](https://www.mongodb.com/)
[![Tests](https://img.shields.io/badge/tests-23%2F23%20passed-success.svg)](tests/test_main.py)

A production-ready, high-performance **Employee Attendance & Analytics API** built with **FastAPI** and **MongoDB 7.0**, developed for the **HROne Software Development Trainee (SDT)** assignment.

It features atomic punch-in and punch-out mechanics under concurrent load, strict attendance business logic (IST timezone attribution, overnight shift detection, grace periods, half-up rounding, overtime thresholds), manual record regularization with immutable audit logging, and high-performance MongoDB aggregation pipelines for organizational analytics.

---

## 🚀 Key Highlights & Architectural Features

- **Race-Free Concurrency Control**: Uses compound unique database indexes (`(emp_code, date)` and `emp_code`) to atomically reject duplicate punch-ins and employee registrations without application-level distributed locks.
- **Strict Business Logic Compliance**:
  - **R1 (Date & Shift Attribution)**: Determines calendar day in IST (+05:30); detects overnight shifts and attributes punches before shift end to the previous calendar day.
  - **R2 (Grace Period)**: 10-minute grace period (`punched_at > 10:00` late). Calculates late duration from shift start.
  - **R3 (Overtime Threshold)**: Overtime counted only when $\ge$ 30 minutes past scheduled shift end.
  - **R4 (Mathematical Half-Up Rounding)**: Implements true half-up rounding to 2/4 decimal places, avoiding Python's default round-half-even (banker's rounding) discrepancies.
  - **R5 (Half-Day Flag)**: Automatically flagged when total working hours are strictly under 4.0 hours.
- **Audit Trail & Regularization**: `PATCH /attendance/{emp_code}/{date}` validates adjustments and records an immutable history of modifications (`changed_by`, `reason`, `changed_at`, `old_values`, `new_values`) using compare-and-swap update semantics.
- **High-Performance MongoDB Aggregations**:
  - Employs `$lookup`, `$densify`, and `$setWindowFields` with `$rank` for standard competition tie-breaking (1, 2, 2, 4).
  - Department summary left-joins logs from the `employees` collection to accurately preserve headcount for staff with zero monthly logs.
- **Production Readiness**:
  - FastAPI `lifespan` context manager creates query-optimized compound indexes at startup.
  - Live database ping probe (`/health`) returns 503 if MongoDB becomes unreachable.
  - Epoch millisecond serialization for all API timestamp contracts.

---

## 🛠️ Tech Stack

- **Language:** Python 3.11+ (Tested on Python 3.13)
- **Framework:** FastAPI
- **Validation & Serialization:** Pydantic v2
- **Database:** MongoDB 6.0+ (Tested on MongoDB 7.0) via PyMongo
- **Testing:** Pytest, pytest-asyncio, AnyIO
- **Configuration:** Python-Dotenv

---

## 📋 API Specification & Endpoints

Interactive Swagger UI documentation is available at: **`http://localhost:8000/docs`**  
ReDoc alternative documentation is available at: **`http://localhost:8000/redoc`**

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Live MongoDB ping readiness probe (returns 200 or 503) |
| `POST` | `/employees` | Register new employee (unique `emp_code` enforcement) |
| `GET` | `/employees` | Paginated employee directory with department filtering |
| `GET` | `/employees/{emp_code}` | Retrieve single employee record |
| `POST` | `/attendance/punch-in` | Punch-in with grace period & overnight shift detection |
| `POST` | `/attendance/punch-out` | Punch-out with work hours, overtime, and half-day calculation |
| `GET` | `/attendance` | Paginated attendance logs sorted by `date DESC, emp_code ASC` |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize attendance record with full audit history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee summary (hours, days, late, overtime) |
| `GET` | `/analytics/departments/summary` | Department-wide monthly aggregations with zero-log headcount |
| `GET` | `/analytics/leaderboard/late` | Late employee leaderboard using MongoDB `$rank` windowing |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trends with 7-day moving averages |
| `GET` | `/admin/explain/{endpoint}` | MongoDB query execution plan explain probe (`executionStats`) |

---

## ⚙️ Quickstart & Local Setup

### 1. Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Local Docker container, local MongoDB service, or free MongoDB Atlas cluster)

### 2. Clone and Setup Environment

```bash
git clone https://github.com/rathoreanuj/HROne-SDT-Assignment---Employee-Attendance-Analytics-API.git
cd HROne-SDT-Assignment---Employee-Attendance-Analytics-API
```

Create and activate a virtual environment:

```bash
# Windows (PowerShell):
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Linux / macOS:
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Create a `.env` file in the project root:

```env
# MongoDB Connection Configuration
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(If connecting to MongoDB Atlas, replace `MONGO_URI` with your `mongodb+srv://...` connection string).*

### 4. Start MongoDB (via Docker)
If using Docker Desktop:

```bash
docker run -d -p 27017:27017 --name mongo7 mongo:7
```

### 5. Seed the Sample Database
Load sample employees and attendance logs:

```bash
python sample_seed.py
```
*(Output: `loaded 6 documents into 'attendance_db.employees'`, `loaded 9 documents into 'attendance_db.attendance_logs'`)*

### 6. Run the FastAPI Application

```bash
uvicorn app.main:app --port 8000 --reload
```
API server will run at: **`http://localhost:8000`**

---

## 🧪 Running Automated Unit Tests

A comprehensive unit test suite is included in [`tests/test_main.py`](tests/test_main.py). Tests execute without external database dependencies by targeting pure business logic functions:

```bash
pytest tests/test_main.py -v
```

### Test Coverage Highlights:
- **`TestHalfUp`**: Verifies exact mathematical half-up rounding over positive, negative, and multi-decimal values.
- **`TestLateMinutes`**: Validates boundary conditions (strictly > 10 min late vs exactly 10 min).
- **`TestWorkHours`**: Verifies regular, half-day, and multi-day / overnight shift durations.
- **`TestOvertimeMinutes`**: Verifies 30-minute minimum threshold (29m $\rightarrow$ 0, 30m $\rightarrow$ 30, 45m $\rightarrow$ 45).
- **`TestHalfDay`**: Verifies threshold boundary (< 4.0 hours flagged as true).
- **`TestAttendanceDate`**: Validates overnight shift attribution across midnight in IST.
- **`TestEpochConversions`**: Ensures integer millisecond $\leftrightarrow$ UTC datetime lossless roundtrips.

**Result: 23 / 23 Unit Tests Passing.**

---

## 📁 Repository Deliverables

- [`app/main.py`](app/main.py): Complete API implementation with all routes, schemas, indexes, and business logic.
- [`REVIEW.md`](REVIEW.md): In-depth code review analyzing **17 distinct defects** in the starter code, how each manifests, and how it was fixed.
- [`DECISIONS.md`](DECISIONS.md): Architectural decisions covering index selection, punch-in race condition resolution, ranking tie-breaking semantics, zero-log headcount retention, and 100x horizontal scaling strategies.
- [`sample_seed.py`](sample_seed.py): Database seeder script for sample data ingestion.
- [`openapi.yaml`](openapi.yaml): Full OpenAPI 3.0 specification contract.
- [`DATA_MODEL.md`](DATA_MODEL.md): Document schemas and collection structure reference.
