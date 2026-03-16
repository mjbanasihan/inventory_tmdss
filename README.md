# TSD-TMDSS Inventory System

Full-stack inventory management app built with **FastAPI** + **PostgreSQL** + vanilla HTML/CSS/JS. Deployable on [Render](https://render.com).

---

## 📁 Project Structure

```
tsd-inventory/
├── backend/
│   ├── main.py          # FastAPI app, all API routes
│   ├── database.py      # SQLAlchemy engine + session
│   ├── models.py        # DB table definitions
│   └── schemas.py       # Pydantic request/response models
├── frontend/
│   └── index.html       # Full UI (served by FastAPI)
├── requirements.txt
├── render.yaml          # One-click Render deployment config
└── .env.example
```

---

## 🚀 Run Locally

### 1. Clone and install dependencies

```bash
git clone <your-repo-url>
cd tsd-inventory
pip install -r requirements.txt
```

### 2. Set up environment

```bash
cp .env.example .env
# No changes needed — SQLite is used locally by default
```

### 3. Start the server

```bash
uvicorn backend.main:app --reload
```

Open **http://localhost:8000** in your browser.

> API docs available at **http://localhost:8000/docs** (FastAPI auto-generated Swagger UI)

---

## ☁️ Deploy to Render

### Option A — One-click with render.yaml (recommended)

1. Push this project to a GitHub repository
2. Go to [render.com](https://render.com) → **New** → **Blueprint**
3. Connect your GitHub repo
4. Render reads `render.yaml` and automatically:
   - Creates a **PostgreSQL** database (free tier)
   - Creates a **Web Service** running FastAPI
   - Injects `DATABASE_URL` into the environment
5. Click **Apply** — your app is live in ~2 minutes!

### Option B — Manual setup

1. Go to Render → **New** → **PostgreSQL** → create a free database
2. Copy the **Internal Database URL**
3. Go to Render → **New** → **Web Service** → connect your repo
4. Set these:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
   - **Environment Variable:** `DATABASE_URL` = *(paste the Internal Database URL)*
5. Deploy!

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/inventory` | List all inventory items (supports `?search=`) |
| POST | `/api/inventory` | Add new inventory item |
| PUT | `/api/inventory/{id}` | Update an inventory item |
| DELETE | `/api/inventory/{id}` | Delete an inventory item |
| GET | `/api/given-out` | List all given-out items (supports `?search=`) |
| POST | `/api/given-out` | Add new given-out item |
| PUT | `/api/given-out/{id}` | Update a given-out item |
| DELETE | `/api/given-out/{id}` | Delete a given-out item |
| GET | `/api/summary` | Get totals and full list for both tables |
| GET | `/docs` | Interactive API documentation (Swagger UI) |

---

## 🗄️ Database Schema

**inventory_items**
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Primary key |
| supply_name | VARCHAR | Required |
| quantity | INTEGER | Default 0 |
| date_received | VARCHAR | ISO date string (YYYY-MM-DD) |

**given_out_items**
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Primary key |
| supply_name | VARCHAR | Required |
| quantity | INTEGER | Default 0 |
| who_received | VARCHAR | Name of recipient |

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11 + FastAPI |
| ORM | SQLAlchemy 2.0 |
| Validation | Pydantic v2 |
| Database (prod) | PostgreSQL (Render free tier) |
| Database (dev) | SQLite (zero setup) |
| Server | Uvicorn (ASGI) |
| Frontend | Vanilla HTML / CSS / JS |
| Hosting | Render |
