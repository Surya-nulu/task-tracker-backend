# Task & Alarm Management — End-to-End Implementation Roadmap

> **How to use this file:** it is a standalone plan. Copy it into your project as `ROADMAP.md`.
> Nothing in this document touches the `rl_sage` repository. The Task & Alarm app is a **separate project** (suggested path: `~/projects/task_alarm/`).

---

## 0. Context

**Why this plan exists.** You have finished Step 1 (PostgreSQL in Docker, SQLAlchemy 2.0 models for `User` / `Task` / `Alarm`, Alembic initialised). You want to grow that into a full-stack, production-oriented Task & Alarm app — FastAPI REST + JWT auth, a Vue 3 dashboard, Celery/Redis background alarm checking, Redis Pub/Sub → WebSocket real-time delivery, browser notifications + sound, all under Docker Compose — **incrementally, in 19 phases, with explanations**, because you are learning FastAPI, SQLAlchemy, Vue and Celery at the same time.

**What I could and could not inspect.** Your Step 1 project was not reachable from this session (searched `~/projects`, `~/Desktop`, `~/Documents` for `trigger_at` / `is_triggered` / `task_tracker`; Docker daemon was not running). So Phase 1 below is written as a **self-audit** — run the listed commands against *your* project, compare with the expected shape, and apply the corrections. Nothing from Step 1 gets recreated unless the audit shows it is missing.

**Intended outcome.** At the end of Phase 19 the final user flow (register → login → create task + alarm → close browser → Celery fires alarm → Redis → WebSocket → notification + sound → dashboard updates) works end-to-end from `docker compose up`, with tests, consistent error handling, and a documented list of what is dev-only vs production-required.

---

## 1. Final architecture

```
                        ┌─────────────────────────────────────┐
                        │            Vue 3 SPA (Vite)         │
                        │  Vue Router · Pinia · Axios         │
                        │  useWebSocket · useNotifications    │
                        │  useAlarmSound                      │
                        └──────────┬───────────────┬──────────┘
                      HTTP /api/*  │               │  WS /ws  (first-message auth)
                                   ▼               ▼
                        ┌─────────────────────────────────────┐
                        │              FastAPI                │
                        │  routers: auth · tasks · alarms     │
                        │  websocket: /ws + ConnectionManager │
                        │  lifespan: Redis subscriber task    │
                        └──────┬──────────────────────┬───────┘
                               │ SQLAlchemy async      │ redis.asyncio
                               │ (asyncpg)             │ SUBSCRIBE alarm_events
                               ▼                       ▼
                   ┌────────────────────┐    ┌────────────────────┐
                   │     PostgreSQL     │    │        Redis       │
                   │ users tasks alarms │    │ broker (db 0)      │
                   │ (source of truth)  │    │ Pub/Sub channel    │
                   └─────────▲──────────┘    └──────────▲─────────┘
                             │ SQLAlchemy sync (psycopg)│ PUBLISH alarm_events
                             │                          │
                   ┌─────────┴──────────────────────────┴─────────┐
                   │                 Celery Worker                │
                   │  check_due_alarms: claim (SKIP LOCKED) →     │
                   │  commit → publish event per alarm            │
                   └──────────────────────▲───────────────────────┘
                                          │ enqueue every 60 s
                   ┌──────────────────────┴───────────────────────┐
                   │        Celery Beat (exactly ONE replica)      │
                   └──────────────────────────────────────────────┘
```

**The alarm flow (what every later phase serves):**

```
create task ──► create alarm ──► rows in PostgreSQL
                                        │
   Celery Beat tick (60 s) ──► worker: UPDATE alarms … WHERE trigger_at <= now()
                                        AND is_triggered = false
                                        FOR UPDATE SKIP LOCKED  (atomic claim)
                                        │ commit
                                        ▼
                               PUBLISH alarm_events {"event":"alarm_triggered",…}
                                        │
                     every FastAPI instance's subscriber receives it
                                        │
                     ConnectionManager.send_to_user(user_id, payload)
                                        │
                     browser: Pinia update → Notification → sound → UI badge
```

### Repository layout (target)

```
task_alarm/
├── docker-compose.yml
├── docker-compose.override.yml      # dev: bind mounts + hot reload
├── .env.example                     # committed; .env is git-ignored
├── README.md
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py                   # async engine, URL from settings
│   │   └── versions/
│   ├── app/
│   │   ├── main.py                  # create_app(), lifespan, routers, handlers
│   │   ├── core/
│   │   │   ├── config.py            # Settings (pydantic-settings)
│   │   │   ├── database.py          # async engine, AsyncSessionLocal, get_db
│   │   │   ├── security.py          # hash/verify password, create/decode JWT
│   │   │   ├── redis.py             # async Redis client + channel name
│   │   │   ├── exceptions.py        # AppError hierarchy + handlers
│   │   │   └── logging.py
│   │   ├── models/
│   │   │   ├── base.py              # Base + naming convention + TimestampMixin
│   │   │   ├── user.py  task.py  alarm.py
│   │   ├── schemas/
│   │   │   ├── auth.py  task.py  alarm.py  common.py  events.py
│   │   ├── routers/
│   │   │   ├── auth.py  tasks.py  alarms.py  health.py
│   │   ├── services/
│   │   │   ├── auth_service.py  task_service.py  alarm_service.py
│   │   ├── dependencies/
│   │   │   └── auth.py              # get_current_user
│   │   └── websocket/
│   │       ├── manager.py           # ConnectionManager
│   │       ├── router.py            # /ws endpoint
│   │       └── subscriber.py        # Redis → manager bridge
│   ├── worker/
│   │   ├── celery_app.py
│   │   ├── db.py                    # SYNC engine (psycopg) for the worker
│   │   ├── publisher.py             # sync Redis publish
│   │   └── tasks.py                 # check_due_alarms
│   └── tests/
│       ├── conftest.py
│       ├── test_auth.py  test_tasks.py  test_alarms.py
│       ├── test_authorization.py  test_worker.py  test_websocket.py
│
└── frontend/
    ├── Dockerfile                   # multi-stage: node build → nginx
    ├── nginx.conf                   # serves dist, proxies /api and /ws
    ├── vite.config.ts               # dev proxy /api and /ws → :8000
    └── src/
        ├── main.ts  App.vue
        ├── router/index.ts
        ├── services/api.ts          # axios instance + interceptors
        ├── stores/auth.ts  tasks.ts  realtime.ts
        ├── composables/useWebSocket.ts  useNotifications.ts  useAlarmSound.ts
        ├── types/api.ts             # mirrors Pydantic schemas
        ├── views/LoginView.vue  RegisterView.vue  DashboardView.vue
        └── components/TaskList.vue  TaskItem.vue  TaskForm.vue  AlarmForm.vue
                       MissedAlarmsBanner.vue  SoundUnlockBanner.vue
```

**Deviations from your suggested structure, and why**

| Change | Why |
|---|---|
| `worker/` lives **inside `backend/`** (same Python package, same Docker image, different `command:`) | The worker imports `app.models` and `app.core.config`. One image, one `requirements.txt`, zero duplicated model code. |
| Worker uses a **sync** SQLAlchemy engine (`postgresql+psycopg://`) in `worker/db.py` | Celery tasks are synchronous; mixing asyncio event loops with the prefork pool is a classic footgun. Models are driver-agnostic, so only the engine differs. One extra dependency (`psycopg[binary]`). |
| Added `app/websocket/` package | WebSocket concerns (connection registry, auth handshake, Redis bridge) are a cohesive unit, not a router. |
| Added `app/core/exceptions.py` + `schemas/common.py` | Services raise domain errors (`NotFoundError`, `ConflictError`…); one set of handlers maps them to HTTP. Routers stay thin and services stay testable without HTTP. |
| Added `models/base.py` with a **constraint naming convention** and a `TimestampMixin` | Alembic can only `drop_constraint` by name; unnamed constraints bite you in migration #3. |
| All REST routes mounted under **`/api`** (so `POST /api/auth/register`) | One proxy rule in Vite dev and nginx prod; same-origin → no CORS in production. Paths inside are exactly as you specified. Set `API_PREFIX=""` in config if you prefer bare paths. |
| Two small extra endpoints: `POST /alarms/{id}/ack`, `GET /alarms/missed` | Needed for "what if the browser was offline" (decision #9/#12). Everything else is your list. |

---

## 2. Architectural decisions (the 15 questions)

| # | Question | MVP decision | Why | Postponed |
|---|---|---|---|---|
| 1 | Alarm ↔ Task cardinality | **One-to-many** (Task has many alarms). Your schema already supports it (`alarm.task_id` FK). UI may show one alarm per task at first. | "1 day before + 1 hour before" is the obvious next ask; 1:N costs nothing now, a migration later. | Multi-alarm UI polish |
| 2 | Completed task → future alarms | **Yes, suppressed — by the worker's query**, not by mutating alarm rows. Worker joins `tasks` and requires `status = 'pending'`. | No migration, no data loss; re-opening a task re-enables its alarms automatically. | "Cancelled" alarm state |
| 3 | Recurring alarms | **Postpone.** | Needs `recurrence_rule` + next-occurrence computation and complicates the idempotency story. | Phase 20+ |
| 4 | Timezone handling | API accepts/returns **ISO-8601 with offset** (Pydantic `AwareDatetime` rejects naive values). Browser converts `datetime-local` → `toISOString()`; displays with `toLocaleString()`. Server never guesses a user's timezone. | The browser is the only component that knows the user's zone. | `users.timezone` column (for emails) |
| 5 | Store everything in UTC? | **Yes.** `DateTime(timezone=True)` → `timestamptz`; always `datetime.now(timezone.utc)`; never `datetime.utcnow()` (naive, deprecated). | Comparisons like `trigger_at <= now()` are only safe when both sides are aware UTC. | — |
| 6 | Celery firing the same alarm twice | **Atomic claim in one statement:** `UPDATE alarms SET is_triggered=true, triggered_at=now() WHERE id IN (SELECT id FROM alarms JOIN tasks … WHERE trigger_at<=now() AND is_triggered=false … FOR UPDATE SKIP LOCKED LIMIT :n) RETURNING …` then **commit, then publish**. Beat task gets `expires=50` so backlog ticks don't pile up. | Even two overlapping workers cannot both claim the same row. The DB flag *is* the idempotency key. | Outbox table for exactly-once publish |
| 7 | Redis Pub/Sub ↔ WebSockets | FastAPI `lifespan` starts one background task per process: `SUBSCRIBE alarm_events` → `ConnectionManager.send_to_user()`. Pub/Sub is fire-and-forget; PostgreSQL remains the truth. | Decouples worker from web process; scales to N web replicas for free. | Redis Streams (durable) |
| 8 | WebSocket auth | **`/ws` + first-message auth:** client sends `{"type":"auth","token":"…"}` within 5 s; server validates JWT, registers socket, replies `auth_ok`; else closes with code `4401`. **Not** `/ws/{user_id}`, **not** `?token=` in the URL. | Tokens in URLs leak into proxy/server logs and browser history. Browsers can't set custom headers on WS, so the first message is the cleanest portable option. Tradeoff: ~5 s window of an unauthenticated open socket (bounded by the timeout). | One-time ticket (`POST /auth/ws-ticket` → `/ws?ticket=`) |
| 9 | Browser offline when alarm fires | Alarm is still marked triggered; the Pub/Sub message is dropped. On (re)connect the client calls **`GET /api/alarms/missed`** (triggered, unacknowledged, task still pending) and shows a "Missed alarms" banner; **`POST /api/alarms/{id}/ack`** clears it. | DB is the inbox; the socket is only a fast path. | Push notifications / email |
| 10 | WebSocket disconnects | Client reconnects with exponential backoff + jitter (1 s → 30 s cap), re-authenticates, then **reconciles** (refetch tasks + missed alarms). Server drops sockets that fail on send. | Reconnect without reconcile silently loses events. | — |
| 11 | Celery temporarily down | Alarms accumulate as `is_triggered=false` with past `trigger_at`. First tick after recovery claims them all (batched, `ALARM_BATCH_SIZE`), event carries original `trigger_at` so the UI can label "late". | Simple and honest; nothing is lost. | Suppress alarms older than N hours |
| 12 | Missed alarms | Covered by 9 + 11: `is_triggered AND acknowledged_at IS NULL` ⇒ missed/unseen. | One rule, one query, one banner. | — |
| 13 | Persist notifications in PostgreSQL? | **Not as a separate table.** The alarm row *is* the notification record (`is_triggered`, `triggered_at`, `acknowledged_at`). | Avoids a parallel table that duplicates alarm state. | `notifications` table when multi-channel |
| 14 | Multiple FastAPI instances | Already works: every instance subscribes to Redis; each delivers to **its own** sockets. No sticky sessions. **Celery Beat must be a single replica.** | Pub/Sub broadcasts; the manager is per-process by design. | — |
| 15 | Migrations in Docker | A one-shot **`migrate` service** (`alembic upgrade head`) that `depends_on: postgres (healthy)`; `backend` depends on `migrate: service_completed_successfully`. Never `create_all` at startup, never autogenerate in a container. | Running migrations inside app startup races with N replicas. | CI job that runs migrations |

Supporting choices:

| Topic | Choice | Why |
|---|---|---|
| Password hashing | `bcrypt` library directly (`hashpw`/`checkpw`) | `passlib` is unmaintained and breaks with `bcrypt>=4.1` (`AttributeError: module 'bcrypt' has no attribute '__about__'`). Enforce `max_length=64` on passwords (bcrypt truncates at 72 bytes). |
| JWT | `PyJWT`, HS256, claims `sub` (user id **as string**), `iat`, `exp`, `type:"access"` | `python-jose` is poorly maintained; PyJWT ≥ 2.10 requires `sub` to be a string. |
| Swagger auth | `HTTPBearer` security scheme (paste the token) | Matches your flow "Copy JWT → Authorize"; login stays a JSON endpoint; no `python-multipart` needed. |
| `Task.status` storage | `String(20)` + Python `enum.Enum` validated by Pydantic (+ CHECK constraint) | Native PG enums need `ALTER TYPE` for every new value. |
| Cross-user access | **404**, not 403 | Avoids confirming that another user's resource exists (enumeration). 403 is reserved for "it's yours but you may not do that". |
| Celery result backend | **None** (`task_ignore_result=True`) | We never read results; a backend only clutters Redis. |
| Pagination for `GET /tasks` | `limit` (≤100, default 50) + `offset`, ordered by `created_at DESC, id DESC`, optional `status` filter | Deterministic ordering; enough for an MVP. |

---

## 3. Working agreement for every phase

For each phase we will: (1) explain what we are doing and why; (2) make the changes; (3) walk through each important file and code section; (4) run the exact commands; (5) compare against the expected output; (6) verify with the "How to test" steps; (7) tick the completion checklist. **We do not start the next phase until the checklist is fully green.**

Every phase below has the same 13 sections: Goal · Architecture changes · Files to create · Files to modify · Database changes · API endpoints · Implementation steps · Commands · Expected output · How to test · Common errors · Security considerations · Completion checklist.

---

## PHASE 1 — Existing PostgreSQL + SQLAlchemy + Alembic (self-audit)

**Goal.** Confirm Step 1 is in the shape the remaining phases depend on, and fix the handful of things that are much cheaper to fix now than after there is data and three migrations.

**Architecture changes.** None functionally. Introduce `models/base.py` (naming convention + timestamp mixin) and make `alembic/env.py` async + settings-driven.

**Files to create.**
- `backend/app/models/base.py`
- `backend/.env.example`

**Files to modify.**
- `backend/app/models/{user,task,alarm}.py` — apply the corrections table below
- `backend/alembic/env.py` — async engine, `target_metadata = Base.metadata`, URL from `Settings`
- `backend/alembic.ini` — remove the hardcoded `sqlalchemy.url` (env.py sets it)
- `backend/.gitignore` — `.env`, `__pycache__/`, `.venv/`

**Database changes.** Possibly a corrective migration (see audit). If your DB is still empty, it is acceptable to delete the existing version file(s), drop the DB, and regenerate a clean initial migration — **only** because there is no data yet.

**API endpoints.** None.

**Implementation steps.**

1. Run the audit commands (below) and compare every line with this expected shape:

```python
# models/base.py (shape)
from sqlalchemy import MetaData, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
```

```python
# models (expected end state after Phases 1/4/5)
class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    tasks: Mapped[list["Task"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user: Mapped["User"] = relationship(back_populates="tasks")
    alarms: Mapped[list["Alarm"]] = relationship(back_populates="task", cascade="all, delete-orphan", lazy="selectin")
    __table_args__ = (CheckConstraint("status IN ('pending','completed')", name="status_valid"),)

class Alarm(Base, TimestampMixin):
    __tablename__ = "alarms"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    trigger_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_triggered: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))      # Phase 5
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # Phase 5
    task: Mapped["Task"] = relationship(back_populates="alarms")
    __table_args__ = (Index("ix_alarms_due", "trigger_at", postgresql_where=text("is_triggered = false")),)  # Phase 5
```

2. Apply the **corrections** that apply to your code:

| Check | Why it matters |
|---|---|
| `due_at` / `trigger_at` are `DateTime(timezone=True)` | Without it Postgres stores `timestamp` (naive) and `trigger_at <= now()` compares apples to oranges. |
| `users.email` is `unique=True` + indexed; store lowercased | Duplicate-registration check must be enforced by the DB, not just the app. |
| `tasks.user_id`, `alarms.task_id` have `ondelete="CASCADE"` and an index | Deleting a user/task must not leave orphans; FK columns are always filtered on. |
| `Task.alarms` relationship uses `lazy="selectin"` | **Async SQLAlchemy cannot lazy-load.** Accessing `task.alarms` without eager loading raises `MissingGreenlet`. `selectin` loads alarms in one extra query per batch. |
| `status` default `"pending"`, `is_triggered` default `false`, both `nullable=False` | Nullable booleans/status create three-state bugs. |
| `Base` has a naming convention | Alembic needs constraint names to drop/alter them later. |
| `alembic/env.py` reads `DATABASE_URL` from `Settings`, uses `async_engine_from_config` + `connection.run_sync(do_run_migrations)` | No credentials in `alembic.ini`; one source of truth for the URL. |
| `TimestampMixin` on all three models | Needed for sorting and audit (Phase 4 question answered: **yes, add `created_at`/`updated_at` via migration**). |

3. If a corrective migration is needed: `alembic revision --autogenerate -m "phase1 corrections"` → **read the generated file** → `alembic upgrade head`.

**Commands.**
```bash
cd ~/projects/task_alarm/backend
tree -I '__pycache__|.venv' -L 3
sed -n '1,200p' app/models/*.py
sed -n '1,120p' alembic/env.py
grep -n sqlalchemy.url alembic.ini
alembic current && alembic heads && alembic history
docker ps --filter name=postgres
psql "$DATABASE_URL_SYNC" -c '\d+ users' -c '\d+ tasks' -c '\d+ alarms'   # or docker exec … psql
```

**Expected output.** `alembic current` shows exactly one revision = `alembic heads`; `\d+ alarms` shows `trigger_at | timestamp with time zone`; FK rows show `ON DELETE CASCADE`; indexes on `users.email`, `tasks.user_id`, `alarms.task_id`.

**How to test.** `python -c "from app.models import User, Task, Alarm; print(Task.__table__.c.due_at.type)"` prints `DateTime(timezone=True)`. `alembic upgrade head` on a freshly dropped DB succeeds; `alembic check` (Alembic ≥1.9) reports no pending changes.

**Common errors.** `Target database is not up to date` → run `alembic upgrade head` first. Autogenerate produces an empty migration → `target_metadata` is not `Base.metadata` or models aren't imported in `env.py`. `asyncpg` + `alembic` sync `env.py` → `MissingGreenlet`/`greenlet_spawn` errors — switch to the async template.

**Security considerations.** `.env` git-ignored from day one; `.env.example` has placeholder values only.

**Completion checklist.**
- [ ] Models match the expected shape (tz-aware datetimes, cascades, indexes, defaults, naming convention, selectin relationship)
- [ ] `alembic/env.py` is async and reads the URL from settings; `alembic.ini` has no credentials
- [ ] One linear migration head; `alembic upgrade head` from scratch works
- [ ] `.env` ignored; `.env.example` committed

---

## PHASE 2 — FastAPI foundation

**Goal.** A running FastAPI app with settings, async DB session dependency, consistent error handling, CORS, a `/api/health` endpoint, and Swagger at `/docs` — no business logic yet.

**Architecture changes.** Introduce `core/config.py`, `core/database.py`, `core/exceptions.py`, `core/logging.py`, `schemas/common.py`, `routers/health.py`, `main.py` with `create_app()` + `lifespan`.

**Files to create.**
- `app/core/config.py`
- `app/core/database.py`
- `app/core/exceptions.py`
- `app/core/logging.py`
- `app/schemas/common.py`
- `app/routers/health.py`
- `app/main.py`
- `requirements.txt`, `.env` (from `.env.example`)

**Files to modify.** `alembic/env.py` (import `settings` from `app.core.config` if not done in Phase 1).

**Database changes.** None.

**API endpoints.** `GET /api/health` → `{"status":"ok","database":"ok"}` (runs `SELECT 1`).

**Implementation steps.**

1. **Settings** (`core/config.py`) — one `Settings(BaseSettings)` with `model_config = SettingsConfigDict(env_file=".env")`:
   `DATABASE_URL`, `JWT_SECRET`, `JWT_ALGORITHM="HS256"`, `ACCESS_TOKEN_EXPIRE_MINUTES=60`, `REDIS_URL="redis://localhost:6379/0"`, `CELERY_BROKER_URL` (defaults to `REDIS_URL`), `CORS_ORIGINS=["http://localhost:5173"]`, `API_PREFIX="/api"`, `ENVIRONMENT="development"`, `ALARM_CHECK_INTERVAL_SECONDS=60`, `ALARM_BATCH_SIZE=100`.
   Add a `@property sync_database_url` that swaps `+asyncpg` → `+psycopg` so the worker (Phase 10) needs no second env var. Expose a cached `get_settings()`.

2. **Database** (`core/database.py`):
   ```python
   engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
   AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

   async def get_db() -> AsyncIterator[AsyncSession]:
       async with AsyncSessionLocal() as session:
           yield session
   ```
   `expire_on_commit=False` is essential in async: otherwise attributes expire after commit and the next access tries to lazy-load → `MissingGreenlet`. Services call `await db.commit()` explicitly; the dependency only owns the session's lifetime.

3. **Errors** (`core/exceptions.py`): `AppError(status_code, code, detail)` base + `NotFoundError(404)`, `ConflictError(409)`, `UnauthorizedError(401)`, `ForbiddenError(403)`, `BadRequestError(400)`. Handlers: `AppError` → `{"detail", "code"}`; `RequestValidationError` → 422 with FastAPI's error list + `code:"validation_error"`; `IntegrityError` → 409; bare `Exception` → log traceback, return `{"detail":"Internal server error","code":"internal_error"}` (never the exception text).

4. **main.py**: `create_app()` builds the app, adds `CORSMiddleware`, registers handlers, includes routers under `settings.API_PREFIX`; `lifespan` logs startup/shutdown (Redis subscriber plugs in here in Phase 12). Set `title`, `version`, `openapi_tags`.

**Commands.**
```bash
cd backend && python -m venv .venv && source .venv/bin/activate
pip install "fastapi>=0.115" "uvicorn[standard]" "sqlalchemy[asyncio]>=2.0" asyncpg alembic \
            "pydantic>=2.7" pydantic-settings "email-validator" pyjwt bcrypt
pip freeze > requirements.txt
uvicorn app.main:app --reload --port 8000
curl -s localhost:8000/api/health
```

**Expected output.** `INFO: Application startup complete.`; `curl` → `{"status":"ok","database":"ok"}`; `http://127.0.0.1:8000/docs` shows the `health` tag.

**How to test.** Stop Postgres → `/api/health` returns 500 with the generic envelope (and the traceback appears only in the server log). Send `GET /api/nope` → 404 JSON envelope.

**Common errors.** `pydantic_settings` not finding `.env` → run uvicorn from `backend/`. `ValidationError: JWT_SECRET field required` → `.env` missing. `asyncpg.exceptions.InvalidPasswordError` → URL credentials.

**Security considerations.** Secrets only in `.env`; `ENVIRONMENT != production` enables `/docs`; generic 500 body.

**Completion checklist.**
- [ ] `uvicorn app.main:app --reload` starts cleanly from `backend/`
- [ ] `/api/health` reports DB ok; `/docs` renders
- [ ] Unknown route and forced exception both return the JSON error envelope without internals
- [ ] `requirements.txt` committed; no secrets in code

---

## PHASE 3 — Authentication

**Goal.** Register, login (JWT), `GET /auth/me`, and a reusable `get_current_user` dependency.

**Architecture changes.** `core/security.py` (hashing + JWT), `services/auth_service.py`, `dependencies/auth.py`, `routers/auth.py`, `schemas/auth.py`.

**Files to create.** `app/core/security.py`, `app/schemas/auth.py`, `app/services/auth_service.py`, `app/dependencies/auth.py`, `app/routers/auth.py`.

**Files to modify.** `app/main.py` (include router, tag `auth`).

**Database changes.** None (users table exists).

**API endpoints.**

| Method | Path | Body → Response | Codes |
|---|---|---|---|
| POST | `/api/auth/register` | `{email, password}` → `UserRead {id, email, created_at}` | 201, 409 duplicate, 422 |
| POST | `/api/auth/login` | `{email, password}` → `{access_token, token_type:"bearer", expires_in}` | 200, 401 |
| GET | `/api/auth/me` | — → `UserRead` | 200, 401 |

**Implementation steps.**

1. `schemas/auth.py`: `RegisterRequest(email: EmailStr, password: str = Field(min_length=8, max_length=64))`, `LoginRequest`, `TokenResponse`, `UserRead(model_config=ConfigDict(from_attributes=True))` — **never** includes `hashed_password`.
2. `core/security.py`: `hash_password(plain) -> str` (`bcrypt.hashpw(plain.encode(), bcrypt.gensalt())`), `verify_password`, `create_access_token(user_id) -> str` (claims `sub=str(user_id)`, `iat`, `exp=now+ACCESS_TOKEN_EXPIRE_MINUTES`, `type="access"`), `decode_access_token(token) -> int` raising `UnauthorizedError` on `ExpiredSignatureError` / `InvalidTokenError` / wrong `type`.
3. `services/auth_service.py`: `register(db, email, password)` — normalise email to lowercase, check existing → `ConflictError("Email already registered")`, insert, commit, return user. `authenticate(db, email, password)` — fetch by email; **always** run `verify_password` even if user is missing (compare against a dummy hash) to keep timing uniform; raise `UnauthorizedError("Invalid email or password")` (same message for both failure kinds).
4. `dependencies/auth.py`:
   ```python
   bearer = HTTPBearer(auto_error=False)
   async def get_current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: AsyncSession = Depends(get_db)) -> User:
       if creds is None: raise UnauthorizedError("Not authenticated")
       user_id = decode_access_token(creds.credentials)
       user = await db.get(User, user_id)
       if user is None: raise UnauthorizedError("User no longer exists")
       return user
   ```
   `CurrentUser = Annotated[User, Depends(get_current_user)]` for clean router signatures.
5. `routers/auth.py` — thin: parse schema → call service → return schema.

**Commands.**
```bash
curl -s -X POST localhost:8000/api/auth/register -H 'content-type: application/json' -d '{"email":"a@x.com","password":"password123"}'
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login -H 'content-type: application/json' -d '{"email":"a@x.com","password":"password123"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s localhost:8000/api/auth/me -H "Authorization: Bearer $TOKEN"
```

**Expected output.** Register → 201 `{"id":1,"email":"a@x.com",…}`; login → `{"access_token":"eyJ…","token_type":"bearer","expires_in":3600}`; `/me` → the user; `/me` without header → 401 `{"detail":"Not authenticated","code":"unauthorized"}`.

**How to test.** Duplicate register → 409. Wrong password → 401 with the same message as unknown email. Tamper one character of the token → 401. Set `ACCESS_TOKEN_EXPIRE_MINUTES=0` temporarily → `/me` → 401 expired. In Swagger: Authorize → paste token → `/me` works.

**Common errors.** `ValueError: password cannot be longer than 72 bytes` → the `max_length=64` guard. `jwt.exceptions.InvalidSubjectError` → `sub` must be a string. `email-validator` missing → install it. Swagger shows a lock icon but no "Authorize" button → the router dependency isn't using `HTTPBearer`.

**Security considerations.** bcrypt cost default (12); JWT expiry mandatory; uniform login error + uniform timing; email normalised; `hashed_password` never serialised; 401 (not 404) for unknown users on login.

**Completion checklist.**
- [ ] Register/login/me work in curl and Swagger
- [ ] 409 duplicate, 401 bad password, 401 expired/tampered token
- [ ] `UserRead` never leaks `hashed_password`
- [ ] `get_current_user` is the only way routes learn the user id

---

## PHASE 4 — Task CRUD

**Goal.** Owner-scoped CRUD on tasks, plus `PATCH /tasks/{id}/complete`.

**Architecture changes.** `services/task_service.py`, `routers/tasks.py`, `schemas/task.py`. Migration adds `created_at`/`updated_at` (if not already from Phase 1).

**Files to create.** `app/schemas/task.py`, `app/services/task_service.py`, `app/routers/tasks.py`, `alembic/versions/xxxx_task_timestamps.py`.

**Files to modify.** `app/models/task.py` (mixin + check constraint), `app/main.py`.

**Database changes.** `tasks.created_at`, `tasks.updated_at` (`timestamptz`, `server_default now()`), CHECK on `status`, index `(user_id, created_at DESC)` for the list query. **Yes — add them through an Alembic migration**, not by editing the DB by hand: it keeps every environment (dev, test, Docker, CI) reproducible, and `server_default=now()` backfills existing rows for free.

**API endpoints.** (all require Bearer)

| Method | Path | Body → Response | Codes |
|---|---|---|---|
| POST | `/api/tasks` | `TaskCreate{title, due_at?}` → `TaskRead` | 201, 422 |
| GET | `/api/tasks?status=&limit=&offset=` | → `TaskRead[]` (+ header `X-Total-Count`) | 200 |
| GET | `/api/tasks/{id}` | → `TaskRead` | 200, 404 |
| PUT | `/api/tasks/{id}` | `TaskUpdate{title?, due_at?, status?}` → `TaskRead` | 200, 404, 422 |
| PATCH | `/api/tasks/{id}/complete` | → `TaskRead` | 200, 404 |
| DELETE | `/api/tasks/{id}` | → 204 | 204, 404 |

`TaskRead = {id, user_id, title, status, due_at, created_at, updated_at, alarms: AlarmRead[]}`.

**Implementation steps.**
1. `schemas/task.py`: `TaskStatus(str, Enum)` = `pending | completed`; `TaskCreate(title: str = Field(min_length=1, max_length=200), due_at: AwareDatetime | None = None)`; `TaskUpdate` all-optional with `model_config = ConfigDict(extra="forbid")` — **`user_id` is not accepted anywhere**.
2. `services/task_service.py`: every function takes `(db, user_id, …)`; `_get_owned(db, user_id, task_id)` does `select(Task).where(Task.id==task_id, Task.user_id==user_id)` → `NotFoundError` otherwise. `list_tasks` applies status filter, ordering, limit/offset, and a `count` query. `complete_task` sets status and commits.
3. `routers/tasks.py`: `user: CurrentUser` on every route; `user_id=user.id` passed into services. `response_model=TaskRead`, `status_code=201` on create, `204` on delete.

**Commands.**
```bash
curl -s -X POST localhost:8000/api/tasks -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"title":"Complete project report","due_at":"2026-10-02T22:30:00+05:30"}'
curl -s "localhost:8000/api/tasks?status=pending" -H "Authorization: Bearer $TOKEN"
alembic revision --autogenerate -m "task timestamps" && alembic upgrade head
```

**Expected output.** Create → 201 with `due_at` echoed as `2026-10-02T17:00:00Z` (converted to UTC); list → array; `X-Total-Count: 1`.

**How to test.** Register user B; with B's token `GET /api/tasks/1` → **404**. `PUT` with `{"user_id": 2}` → 422 (`extra="forbid"`). `due_at` without offset → 422. Complete → `status: completed`; DELETE → 204; GET → 404.

**Common errors.** `MissingGreenlet` when serialising `alarms` → `lazy="selectin"` missing on `Task.alarms`. `updated_at` not changing → `onupdate=func.now()` only fires when SQLAlchemy issues the UPDATE (fine) — if you used raw `update()` statements, set it explicitly. Autogenerate wants to drop/create the check constraint every time → ensure the constraint `name=` matches the naming convention.

**Security considerations.** `user_id` derived from the JWT only; cross-user → 404; `extra="forbid"` blocks mass assignment; bounds on `title` and `limit`.

**Completion checklist.**
- [ ] All six endpoints verified in curl + Swagger
- [ ] Cross-user read/update/delete → 404
- [ ] `created_at`/`updated_at` present and populated via migration
- [ ] Naive `due_at` rejected; UTC in responses

---

## PHASE 5 — Alarm CRUD

**Goal.** Alarms nested under tasks, ownership enforced through the task, plus the two reconciliation endpoints.

**Architecture changes.** `services/alarm_service.py`, `routers/alarms.py`, `schemas/alarm.py`; migration adds `triggered_at`, `acknowledged_at`, and the partial index the worker will use.

**Files to create.** `app/schemas/alarm.py`, `app/services/alarm_service.py`, `app/routers/alarms.py`, `alembic/versions/xxxx_alarm_tracking.py`.

**Files to modify.** `app/models/alarm.py`, `app/main.py`, `app/schemas/task.py` (import `AlarmRead`).

**Database changes.** `alarms.triggered_at timestamptz NULL`, `alarms.acknowledged_at timestamptz NULL`, partial index `ix_alarms_due ON alarms (trigger_at) WHERE is_triggered = false`. (Why the partial index: the worker's hot query is "untriggered alarms due now"; a partial index stays tiny no matter how many historical alarms exist.)

**API endpoints.**

| Method | Path | Body → Response | Codes |
|---|---|---|---|
| POST | `/api/tasks/{task_id}/alarms` | `AlarmCreate{trigger_at}` → `AlarmRead` | 201, 404 task, 422 past time |
| GET | `/api/tasks/{task_id}/alarms` | → `AlarmRead[]` | 200, 404 |
| PUT | `/api/alarms/{alarm_id}` | `AlarmUpdate{trigger_at}` → `AlarmRead` | 200, 404, 409 already triggered |
| DELETE | `/api/alarms/{alarm_id}` | → 204 | 204, 404 |
| POST | `/api/alarms/{alarm_id}/ack` | → `AlarmRead` (sets `acknowledged_at`) | 200, 404 |
| GET | `/api/alarms/missed` | → `MissedAlarm[] {alarm_id, task_id, title, trigger_at, triggered_at}` | 200 |

`AlarmRead = {id, task_id, trigger_at, is_triggered, triggered_at, acknowledged_at, created_at}`.

**Implementation steps.**
1. `schemas/alarm.py`: `AlarmCreate(trigger_at: AwareDatetime)` with a `field_validator` rejecting times more than 60 s in the past (`"trigger_at must be in the future"`).
2. `alarm_service._get_owned_alarm(db, user_id, alarm_id)`: `select(Alarm).join(Task).where(Alarm.id==alarm_id, Task.user_id==user_id)` → `NotFoundError`. Creating requires `task_service._get_owned` first. Updating a triggered alarm → `ConflictError`. `list_missed` = `is_triggered AND acknowledged_at IS NULL AND Task.status='pending'`, ordered by `triggered_at DESC`, capped at 50.
3. Route order matters: declare `/alarms/missed` **before** `/alarms/{alarm_id}` or FastAPI will try to parse `"missed"` as an int.

**Commands.**
```bash
curl -s -X POST localhost:8000/api/tasks/1/alarms -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "{\"trigger_at\":\"$(date -u -v+2M +%Y-%m-%dT%H:%M:00Z)\"}"      # macOS date; Linux: date -u -d '+2 min'
curl -s localhost:8000/api/alarms/missed -H "Authorization: Bearer $TOKEN"
alembic revision --autogenerate -m "alarm tracking" && alembic upgrade head
```

**Expected output.** 201 `{"id":1,"task_id":1,"trigger_at":"…Z","is_triggered":false,"triggered_at":null,"acknowledged_at":null,…}`; `/missed` → `[]`.

**How to test.** User B cannot `PUT`/`DELETE` A's alarm (404). Past `trigger_at` → 422. `GET /api/tasks/1` now embeds `alarms`. Manually `UPDATE alarms SET is_triggered=true, triggered_at=now()` → `/missed` shows it → `/ack` → `/missed` empty.

**Common errors.** `422 for /alarms/missed` ("value is not a valid integer") → route ordering. Autogenerate misses the partial index → add `postgresql_where=text(...)` in `Index` and check the generated migration includes it.

**Security considerations.** Ownership always resolved through the task join; alarms never accept `task_id` in the body (it comes from the path).

**Completion checklist.**
- [ ] 6 endpoints verified; cross-user → 404
- [ ] Partial index exists (`\d alarms` shows `ix_alarms_due … WHERE (is_triggered = false)`)
- [ ] Task responses embed alarms; missed/ack cycle works

---

## PHASE 6 — Swagger / API testing

**Goal.** Walk the full flow in Swagger exactly as listed, and freeze a Postman/`.http` collection for regression.

**Architecture changes.** None. Add OpenAPI polish: tags, summaries, `responses={401: …, 404: …}` on routes, examples on schema fields.

**Files to create.** `backend/docs/api.http` (VS Code REST Client) or `backend/docs/postman_collection.json`.

**Files to modify.** Routers (docstrings/summaries), `main.py` (`openapi_tags`).

**Database changes / API endpoints.** None.

**Implementation steps.** In `http://127.0.0.1:8000/docs`: 1 Register → 2 Login → 3 copy `access_token` → 4 Authorize → 5 Create task → 6 Get tasks → 7 Update task → 8 Create alarm → 9 Get alarms → 10 Complete task → 11 Delete task. Then repeat 5–11 with a second user to confirm isolation.

**Commands.** `uvicorn app.main:app --reload`; `curl -s localhost:8000/openapi.json | python -m json.tool | head -50`.

**Expected output.** Each step returns the documented code; `openapi.json` lists `auth`, `tasks`, `alarms`, `health` tags and the `HTTPBearer` security scheme.

**How to test.** Every endpoint in Swagger shows example bodies and documented 401/404/409/422 responses; the `.http` file replays the whole flow.

**Common errors.** Swagger "Failed to fetch" → CORS not needed for same origin; usually the server isn't running or the token expired.

**Security considerations.** Plan to disable `/docs` when `ENVIRONMENT=production` (Phase 18).

**Completion checklist.**
- [ ] 11-step flow passes in Swagger for user A; isolation confirmed with user B
- [ ] `.http`/Postman collection committed
- [ ] OpenAPI shows tags, examples, documented error codes

> **Gate:** do not start Phase 7 until Phase 6 is green.

---

## PHASE 7 — Vue 3 frontend

**Goal.** A Vite + Vue 3 + TypeScript app with the three views and components rendering against mock-free API calls (store wiring arrives in Phase 8).

**Architecture changes.** New `frontend/` project; `vite.config.ts` proxies `/api` and `/ws` to `:8000` (no CORS in dev, same shape as nginx in prod).

**Files to create.** Scaffold via `npm create vue@latest`, then: `src/types/api.ts`, `src/services/api.ts`, `src/views/{LoginView,RegisterView,DashboardView}.vue`, `src/components/{TaskList,TaskItem,TaskForm,AlarmForm,MissedAlarmsBanner,SoundUnlockBanner}.vue`, `src/utils/datetime.ts` (`toIsoUtc(localInput)`, `formatLocal(iso)`).

**Files to modify.** `vite.config.ts`, `src/App.vue`, `src/router/index.ts`.

**Database changes / API endpoints.** None.

**Implementation steps.**
1. `npm create vue@latest frontend` → TypeScript ✔, Router ✔, Pinia ✔, ESLint ✔ (others ✘).
2. `types/api.ts` mirrors Pydantic exactly: `UserRead`, `TokenResponse`, `TaskRead`, `TaskStatus`, `AlarmRead`, `MissedAlarm`, `AlarmTriggeredEvent`.
3. `services/api.ts`: `axios.create({ baseURL: "/api" })` (interceptors in Phase 8).
4. Views are presentational; `TaskForm` uses `<input type="datetime-local">` and converts with `toIsoUtc`; `TaskItem` renders `formatLocal(due_at)` and a per-task `AlarmForm`.
5. `vite.config.ts`: `server.proxy = { "/api": { target: "http://localhost:8000" }, "/ws": { target: "ws://localhost:8000", ws: true } }`.

**Commands.** `cd frontend && npm install && npm run dev` → `http://localhost:5173`.

**Expected output.** Login/Register/Dashboard routes render; the dashboard shows "no tasks" (unauthenticated calls 401 — expected until Phase 8).

**How to test.** `npm run type-check` passes; `datetime-local` → `toIsoUtc` produces a `Z`-suffixed string; `formatLocal` shows your wall-clock time.

**Common errors.** Proxy not applied → requests go to `:5173/api` → 404 (restart `vite` after editing config). `datetime-local` returns `""` when empty → guard before converting.

**Security considerations.** No secrets in the frontend; API base is relative.

**Completion checklist.**
- [ ] Scaffold with TS + Router + Pinia; proxy works (`curl localhost:5173/api/health`)
- [ ] All views/components exist and type-check
- [ ] Types file mirrors backend schemas field-for-field

---

## PHASE 8 — Pinia + Router + Axios

**Goal.** Real auth state, token persistence, Axios interceptors, route guards, full task/alarm CRUD from the dashboard, logout.

**Architecture changes.** `stores/auth.ts`, `stores/tasks.ts`; router `beforeEach` guard; Axios request/response interceptors.

**Files to create.** `src/stores/auth.ts`, `src/stores/tasks.ts`.

**Files to modify.** `src/services/api.ts`, `src/router/index.ts`, views/components (wire to stores), `src/main.ts`.

**Database changes / API endpoints.** None.

**Implementation steps.**
1. `stores/auth.ts`: state `token` (from `localStorage` on boot), `user`; actions `register`, `login` (stores token, calls `fetchMe`), `logout` (clears both, disconnects WS later), getter `isAuthenticated`. Explain the storage tradeoff: `localStorage` is readable by XSS; `httpOnly` cookies are the production upgrade (noted in Phase 18).
2. `services/api.ts`: request interceptor adds `Authorization: Bearer …`; response interceptor on **401** → `auth.logout()` + `router.push("/login")`.
3. Router: `meta: { requiresAuth: true }` on `/dashboard`; `beforeEach` redirects to `/login` when unauthenticated, and `/login`→`/dashboard` when already authenticated. On boot, if a token exists, call `fetchMe()` once before the first guarded navigation.
4. `stores/tasks.ts`: `tasks: TaskRead[]`, `missedAlarms`, actions `fetchTasks`, `createTask`, `updateTask`, `deleteTask`, `completeTask`, `createAlarm`, `deleteAlarm`, `fetchMissed`, `ackAlarm`, and `applyAlarmTriggered(event)` (used in Phase 14). Each mutation updates local state from the server response (no optimistic writes for the MVP — simpler to reason about).

**Commands.** `npm run dev`; backend running.

**Expected output.** Register → auto-login → dashboard; refresh keeps you logged in; logout → `/login`; deep-linking `/dashboard` while logged out → `/login`.

**How to test.** Create/edit/complete/delete a task and add/delete an alarm from the UI, then confirm via Swagger. Expire the token (short `ACCESS_TOKEN_EXPIRE_MINUTES`) → the next call bounces you to `/login`.

**Common errors.** Infinite redirect loop → guard runs before `fetchMe` resolves; await it in the guard. Interceptor logs out on *any* 401 including a bad login attempt → exclude `/auth/login` from the interceptor.

**Security considerations.** Token never in URLs; cleared on logout; 401 handled centrally.

**Completion checklist.**
- [ ] Guards + interceptors behave as above
- [ ] Full CRUD + alarm create/delete from the dashboard
- [ ] Refresh persists session; logout clears it

> **Gate:** REST API + Vue dashboard fully working before touching Redis/Celery.

---

## PHASE 9 — Redis

**Goal.** Redis running (Docker), reachable from the backend, with a health check and a shared async client.

**Architecture changes.** `core/redis.py` (async client factory + `ALARM_CHANNEL = "alarm_events"`); `/api/health` also pings Redis.

**Files to create.** `app/core/redis.py`; `docker-compose.yml` gains a `redis` service (or `docker run` for now).

**Files to modify.** `app/routers/health.py`, `app/core/config.py` (already has `REDIS_URL`), `requirements.txt` (`redis>=5`).

**Database changes / API endpoints.** None (health extended).

**Implementation steps.** `redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)` created once in `lifespan`, stored on `app.state.redis`, closed on shutdown. Health: `await redis.ping()`.

**Commands.**
```bash
docker run -d --name task_redis -p 6379:6379 redis:7-alpine
pip install "redis>=5" "celery[redis]" "psycopg[binary]" && pip freeze > requirements.txt
curl -s localhost:8000/api/health
```

**Expected output.** `{"status":"ok","database":"ok","redis":"ok"}`.

**How to test.** `docker stop task_redis` → health reports `redis: "error"` (status 503); start again → ok.

**Common errors.** `redis.exceptions.ConnectionError: Error 61 connecting to localhost:6379` → container not running. Two `redis` packages (`aioredis` legacy) → use only `redis.asyncio`.

**Security considerations.** Redis has no auth in dev; in production bind to the private network only and/or set `requirepass` (Phase 18).

**Completion checklist.**
- [ ] Redis container up; health reports redis ok/error correctly
- [ ] Single shared async client with lifecycle in `lifespan`

---

## PHASE 10 — Celery Worker

**Goal.** A Celery worker that runs `check_due_alarms` once (manually) and claims due alarms safely.

**Architecture changes.** `worker/celery_app.py`, `worker/db.py` (sync engine), `worker/tasks.py`; publishing is a stub until Phase 12.

**Files to create.** `backend/worker/__init__.py`, `celery_app.py`, `db.py`, `tasks.py`.

**Files to modify.** None.

**Database changes.** None (uses Phase 5's partial index).

**API endpoints.** None.

**Implementation steps.**
1. `celery_app.py`: `Celery("task_alarm", broker=settings.CELERY_BROKER_URL)`; config `task_ignore_result=True`, `timezone="UTC"`, `enable_utc=True`, `task_time_limit=55`, `worker_prefetch_multiplier=1`, `include=["worker.tasks"]`.
2. `db.py`: `create_engine(settings.sync_database_url, pool_pre_ping=True)` + `SessionLocal = sessionmaker(engine)`.
3. `tasks.py`:
   ```python
   @celery_app.task(name="worker.check_due_alarms", expires=50)
   def check_due_alarms() -> int:
       now = datetime.now(timezone.utc)
       with SessionLocal() as db, db.begin():
           due_ids = (select(Alarm.id).join(Task)
                      .where(Alarm.trigger_at <= now, Alarm.is_triggered.is_(False), Task.status == "pending")
                      .order_by(Alarm.trigger_at).limit(settings.ALARM_BATCH_SIZE)
                      .with_for_update(skip_locked=True, of=Alarm))
           claimed = db.execute(
               update(Alarm).where(Alarm.id.in_(due_ids))
               .values(is_triggered=True, triggered_at=now)
               .returning(Alarm.id, Alarm.task_id, Alarm.trigger_at)
           ).all()
           events = [build_event(db, row) for row in claimed]   # loads task title + user_id
       # transaction committed here — only now is it safe to publish
       for event in events:
           publish_alarm_event(event)                            # Phase 12
       return len(events)
   ```
   Why it is safe: `FOR UPDATE SKIP LOCKED` lets concurrent workers claim disjoint rows; the `UPDATE … WHERE is_triggered=false` can only succeed once per row; the commit happens **before** publishing so a crash between the two loses at most a notification (recovered by `/alarms/missed`), never produces a duplicate.
   `expires=50`: if the worker was down for an hour, Beat has queued 60 ticks; expired ones are discarded instead of running 60 identical scans.

**Commands.**
```bash
cd backend && celery -A worker.celery_app worker --loglevel=info --concurrency=1
# in another shell:
celery -A worker.celery_app call worker.check_due_alarms
```

**Expected output.** Worker log: `Task worker.check_due_alarms[…] succeeded in 0.0Xs: 1` after you create an alarm whose time has passed.

**How to test.** Create an alarm 1 minute in the past via Swagger (temporarily relax the validator or insert via SQL) → run the task → `is_triggered=true`, `triggered_at` set → run again → returns 0. Complete the task first → alarm is **not** claimed. Run two workers concurrently against 200 due alarms → sum of claims = 200, no duplicates.

**Common errors.** `ModuleNotFoundError: app` → run from `backend/`. `psycopg.OperationalError` → sync URL wrong (check `sync_database_url`). Celery on macOS with prefork: set `--pool=solo` if you see fork safety warnings.

**Security considerations.** Worker uses the same DB credentials — in production give it a role with only `SELECT/UPDATE` on `alarms`/`tasks`/`users`.

**Completion checklist.**
- [ ] Manual task call claims due alarms exactly once
- [ ] Completed tasks' alarms ignored
- [ ] Concurrency test shows no duplicates

---

## PHASE 11 — Celery Beat

**Goal.** `check_due_alarms` runs automatically every minute.

**Architecture changes.** `beat_schedule` in `celery_app.py`.

**Files to create.** None. **Files to modify.** `worker/celery_app.py`.

**Database changes / API endpoints.** None.

**Implementation steps.** `celery_app.conf.beat_schedule = {"check-due-alarms": {"task": "worker.check_due_alarms", "schedule": settings.ALARM_CHECK_INTERVAL_SECONDS}}`. Note the max latency is one interval (60 s); you may lower it to 30 s — it is a float.

**Commands.**
```bash
celery -A worker.celery_app beat --loglevel=info        # separate process from the worker
```

**Expected output.** Beat: `Scheduler: Sending due task check-due-alarms (worker.check_due_alarms)` every 60 s; worker: `succeeded … : 0` (or N).

**How to test.** Create an alarm 2 minutes ahead from the UI, watch the worker log claim it within ≤60 s after the time passes.

**Common errors.** `celerybeat-schedule` file permission/corruption → delete it (it's a cache). Two Beat processes → duplicate ticks (harmless thanks to the claim query, but wasteful) — always one.

**Security considerations.** None new.

**Completion checklist.**
- [ ] Beat ticks every interval; alarms fire within one interval of their time
- [ ] Exactly one Beat process documented as a rule

---

## PHASE 12 — Redis Pub/Sub

**Goal.** Worker publishes `alarm_triggered` events; FastAPI subscribes and logs them (WebSocket delivery is Phase 13).

**Architecture changes.** `worker/publisher.py` (sync `redis.Redis.from_url(...).publish`), `app/websocket/subscriber.py` (async listener started in `lifespan`), `schemas/events.py` (`AlarmTriggeredEvent`).

**Files to create.** `worker/publisher.py`, `app/websocket/subscriber.py`, `app/schemas/events.py`.

**Files to modify.** `worker/tasks.py` (call the publisher), `app/main.py` (start/stop subscriber task).

**Database changes / API endpoints.** None.

**Implementation steps.**
1. Event schema (validated on both sides):
   ```json
   {"event":"alarm_triggered","alarm_id":123,"task_id":456,"user_id":789,
    "title":"Complete project report","trigger_at":"2026-10-02T17:00:00Z","triggered_at":"2026-10-02T17:00:31Z"}
   ```
2. Subscriber (per process):
   ```python
   async def run_subscriber(redis, manager):
       while True:
           try:
               pubsub = redis.pubsub(ignore_subscribe_messages=True)
               await pubsub.subscribe(ALARM_CHANNEL)
               async for msg in pubsub.listen():
                   event = AlarmTriggeredEvent.model_validate_json(msg["data"])
                   await manager.send_to_user(event.user_id, event.model_dump(mode="json"))
           except asyncio.CancelledError: raise
           except Exception: log.exception("subscriber crashed; retrying in 2s"); await asyncio.sleep(2)
   ```
   Started with `asyncio.create_task` in `lifespan`, cancelled on shutdown.

**Commands.**
```bash
redis-cli SUBSCRIBE alarm_events          # watch raw events
redis-cli PUBLISH alarm_events '{"event":"alarm_triggered","alarm_id":1,"task_id":1,"user_id":1,"title":"x","trigger_at":"2026-10-02T17:00:00Z","triggered_at":"2026-10-02T17:00:31Z"}'
```

**Expected output.** FastAPI log: `alarm_triggered for user 1 (alarm 1)`; `redis-cli SUBSCRIBE` shows the worker's real events when alarms fire.

**How to test.** Stop Redis while FastAPI runs → subscriber logs retries, no crash; start Redis → events resume. Publish malformed JSON → logged + skipped.

**Common errors.** Subscriber never receives → different `REDIS_URL` DB index is fine for Pub/Sub (channels are global) but a different **host** is not. `RuntimeError: Event loop is closed` on shutdown → cancel the task and `await pubsub.aclose()` before closing the client.

**Security considerations.** Only trusted processes can publish (private network); the subscriber validates the payload schema before fan-out.

**Completion checklist.**
- [ ] Worker events visible in `redis-cli SUBSCRIBE`
- [ ] FastAPI subscriber logs them and survives Redis restarts

---

## PHASE 13 — FastAPI WebSockets

**Goal.** Authenticated `/ws` endpoint; events reach only the right user's sockets.

**Architecture changes.** `app/websocket/manager.py` (`ConnectionManager`), `app/websocket/router.py` (`/ws`), subscriber now calls `manager.send_to_user`.

**Files to create.** `app/websocket/manager.py`, `app/websocket/router.py`.

**Files to modify.** `app/main.py` (include WS router — **not** under `API_PREFIX`; path is `/ws`), `app/websocket/subscriber.py`.

**Database changes / API endpoints.** WebSocket `/ws`. Protocol:

| Direction | Message |
|---|---|
| client → server (≤5 s after connect) | `{"type":"auth","token":"<jwt>"}` |
| server → client | `{"type":"auth_ok","user_id":1}` or close `4401` |
| client → server every 25 s | `{"type":"ping"}` → `{"type":"pong"}` |
| server → client | `{"type":"alarm_triggered", …event fields}` |

**Implementation steps.**
1. `ConnectionManager`: `self._conns: dict[int, set[WebSocket]]`, `asyncio.Lock`; `connect(user_id, ws)`, `disconnect(user_id, ws)`, `send_to_user(user_id, payload)` iterates a **copy** of the set and removes sockets whose `send_json` raises.
2. Endpoint: `await ws.accept()` → `msg = await asyncio.wait_for(ws.receive_json(), 5)` → validate type/token via `decode_access_token` → check user exists → `manager.connect` → `auth_ok` → loop `receive_json` handling `ping`; `WebSocketDisconnect` → `manager.disconnect`. Any auth failure → `await ws.close(code=4401)`.
3. **Security tradeoffs (explain in code comments):** first-message auth keeps tokens out of URLs/logs; the cost is a short unauthenticated window bounded by the timeout and per-IP limits at the proxy. The ticket approach (one-time, 30 s TTL in Redis, exchanged over HTTPS) closes that window and is the production upgrade. `/ws/{user_id}` without auth is never acceptable — anyone could subscribe to anyone.

**Commands.**
```bash
pip install websockets   # for the CLI client
python -m websockets ws://localhost:8000/ws
> {"type":"auth","token":"<TOKEN>"}
```

**Expected output.** `{"type":"auth_ok","user_id":1}`; publishing an event for `user_id:1` via `redis-cli` appears in this client; an event for `user_id:2` does not.

**How to test.** Connect without sending auth → closed `4401` after 5 s. Bad token → `4401`. Two tabs for the same user → both receive. Kill one client → manager size decreases (log it).

**Common errors.** `WebSocket is not connected. Need to call "accept" first` → accept before receive. Vite proxy for WS needs `ws: true`. Starlette raises `RuntimeError` if you send after close → guard in `send_to_user`.

**Security considerations.** JWT validated per connection; user existence re-checked; payload fan-out keyed by server-side `user_id` (never client-provided).

**Completion checklist.**
- [ ] Auth handshake + close codes behave as specified
- [ ] Events delivered only to the matching user; multi-tab works
- [ ] Dead sockets cleaned up

---

## PHASE 14 — Vue WebSocket client

**Goal.** `useWebSocket()` composable + `realtime` store: connect after login, reconnect with backoff, dispatch `alarm_triggered` into Pinia, reconcile on reconnect.

**Architecture changes.** `composables/useWebSocket.ts`, `stores/realtime.ts` (status, lastEvent), hooks into `auth.login/logout`.

**Files to create.** `src/composables/useWebSocket.ts`, `src/stores/realtime.ts`.

**Files to modify.** `src/stores/auth.ts` (connect on login / boot, disconnect on logout), `src/stores/tasks.ts` (`applyAlarmTriggered`), `DashboardView.vue` (status indicator + banners).

**Database changes / API endpoints.** None.

**Implementation steps.**
1. URL: `` `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws` `` (proxied in dev and prod).
2. `connect()`: open socket → on `open` send `auth` → on `auth_ok` set `status="connected"`, reset backoff, **then** `tasks.fetchTasks()` + `tasks.fetchMissed()` (reconcile) → start 25 s ping interval.
3. `onmessage`: `JSON.parse` in try/catch; `switch (msg.type)`: `alarm_triggered` → `tasks.applyAlarmTriggered(msg)` (marks alarm triggered in state, pushes to `missedAlarms` until acked) + `notify(msg)` + `playAlarm()` (Phase 15); `pong` → ignore.
4. `onclose`: if `code === 4401` → `auth.logout()`; else schedule reconnect `min(30000, 1000 * 2**attempt) + jitter` unless logout was intentional.
5. Pause/resume on `document.visibilitychange` is optional; browsers throttle timers in background tabs, so keep pings ≥ 20 s.

**Commands.** `npm run dev`; full backend + worker + beat + redis running.

**Expected output.** Dashboard shows "● Live"; creating an alarm 1 min ahead → within ~60 s the task row shows "Alarm fired" and the missed banner lists it until acknowledged.

**How to test.** Stop the backend → indicator "reconnecting…", restart → reconnects and refetches. Fire an alarm while the tab is closed → reopen → banner shows the missed alarm (reconcile path). Expired token → socket closes `4401` → redirected to login.

**Common errors.** Reconnect storm → missing backoff cap/intentional-close flag. Duplicate sockets after HMR → keep the socket in a module-level singleton and close it in `import.meta.hot.dispose`.

**Security considerations.** Token sent only inside the socket after connect (never in URL); logout always closes the socket.

**Completion checklist.**
- [ ] Connect/reconnect/backoff/reconcile verified
- [ ] `alarm_triggered` updates Pinia and the UI
- [ ] 4401 → logout path works

---

## PHASE 15 — Browser notifications + alarm sound

**Goal.** Native notification + audible alarm when an event arrives, handled gracefully where permissions/autoplay are restricted.

**Architecture changes.** `composables/useNotifications.ts`, `composables/useAlarmSound.ts`, `SoundUnlockBanner.vue`.

**Files to create.** The two composables + banner component; optional `public/sounds/alarm.mp3` (or synthesise with Web Audio — no asset needed).

**Files to modify.** `AlarmForm.vue` (request permission on first alarm creation), `DashboardView.vue` (banners, settings strip showing notification + sound status), `useWebSocket.ts` (call `notify` + `playAlarm`).

**Database changes / API endpoints.** None.

**Implementation steps.**
1. **Permission timing:** call `Notification.requestPermission()` from the click handler of **"Save alarm"** the first time (contextual, user-initiated — Safari requires a gesture). Store the result in the `realtime` store; show a small status chip ("Notifications: granted/denied/default"); never prompt again automatically. If `"denied"` → fall back to an in-app toast + title flash.
2. `notify(event)`: `if ("Notification" in window && Notification.permission === "granted") new Notification("Task Alarm", { body: event.title, tag: \`alarm-${event.alarm_id}\` })` (the `tag` collapses duplicates across tabs). Also always show the in-app toast.
3. **Autoplay policy, explained:** browsers block audio until the page has received a user gesture (click/keypress). An `audio.play()` before that rejects with `NotAllowedError`. Strategy: `useAlarmSound.unlock()` is called from the login button click and from a "Enable alarm sound" banner: it creates one `AudioContext`, calls `resume()`, and plays a silent buffer — after that, programmatic playback is allowed for the page's lifetime. The unlocked flag is **not** persisted (every page load needs a new gesture). `playAlarm()` synthesises a 3-beep pattern with an `OscillatorNode` (no file, no license) and, if `play()`/`resume()` rejects, shows `SoundUnlockBanner` ("Alarm fired — click to hear it") that replays on click.
4. Reduce-motion/vibration: optional `navigator.vibrate?.([200,100,200])` on mobile.

**Commands.** `npm run dev` on `http://localhost:5173` (notifications work on `localhost`; elsewhere they require HTTPS).

**Expected output.** First alarm creation → permission prompt; when an alarm fires: OS notification "Task Alarm — Complete project report", beeps, toast, task row updates.

**How to test.** Deny permission → toast only, no errors. Load the page and **don't click anything**, fire an alarm → banner appears instead of sound; click it → sound plays. Two tabs → one notification (tag). Test Chrome + Safari + Firefox.

**Common errors.** `NotAllowedError: play() failed because the user didn't interact` → unlock path missing. `Notification` undefined (older Safari iOS) → feature-detect. Sound plays twice → both the toast and the WS handler call `playAlarm`.

**Security considerations.** Never include sensitive data in notification bodies beyond the task title.

**Completion checklist.**
- [ ] Permission requested once, contextually; status visible
- [ ] Sound unlock + fallback banner verified; no uncaught rejections
- [ ] Works (or degrades gracefully) in Chrome, Firefox, Safari

---

## PHASE 16 — Docker Compose integration

**Goal.** `docker compose up --build` brings up postgres, redis, migrate, backend, celery_worker, celery_beat, frontend with health checks and correct ordering.

**Architecture changes.** `backend/Dockerfile`, `frontend/Dockerfile` (+ `nginx.conf`), `docker-compose.yml` (prod-like), `docker-compose.override.yml` (dev: bind mounts, `--reload`, Vite dev server).

**Files to create.** `docker-compose.yml`, `docker-compose.override.yml`, `backend/Dockerfile`, `backend/.dockerignore`, `frontend/Dockerfile`, `frontend/nginx.conf`, `frontend/.dockerignore`, root `.env.example`.

**Files to modify.** `backend/app/core/config.py` (defaults assume service names when `ENVIRONMENT=docker`? — no: **always read from env**, compose supplies `postgres`/`redis` hostnames).

**Database changes.** Applied by the `migrate` service.

**API endpoints.** None.

**Implementation steps.**
```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment: { POSTGRES_USER: ${POSTGRES_USER}, POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}, POSTGRES_DB: ${POSTGRES_DB} }
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"], interval: 5s, timeout: 3s, retries: 10 }
  redis:
    image: redis:7-alpine
    healthcheck: { test: ["CMD", "redis-cli", "ping"], interval: 5s, timeout: 3s, retries: 10 }
  migrate:
    build: ./backend
    command: alembic upgrade head
    env_file: .env
    depends_on: { postgres: { condition: service_healthy } }
  backend:
    build: ./backend
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    env_file: .env
    depends_on: { migrate: { condition: service_completed_successfully }, redis: { condition: service_healthy } }
    healthcheck: { test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health')"], interval: 10s, retries: 5 }
  celery_worker:
    build: ./backend
    command: celery -A worker.celery_app worker --loglevel=info --concurrency=2
    env_file: .env
    depends_on: { migrate: { condition: service_completed_successfully }, redis: { condition: service_healthy } }
  celery_beat:
    build: ./backend
    command: celery -A worker.celery_app beat --loglevel=info
    env_file: .env
    deploy: { replicas: 1 }
    depends_on: [celery_worker]
  frontend:
    build: ./frontend
    ports: ["8080:80"]
    depends_on: { backend: { condition: service_healthy } }
volumes: { pgdata: {} }
```
`.env` for compose: `DATABASE_URL=postgresql+asyncpg://app:secret@postgres:5432/task_tracker`, `REDIS_URL=redis://redis:6379/0` — **service names, never localhost**. `nginx.conf`: `location /api { proxy_pass http://backend:8000; }` and `location /ws { proxy_pass http://backend:8000; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; proxy_read_timeout 3600s; }`. Backend image: `python:3.12-slim`, non-root user, `pip install -r requirements.txt`, `COPY app worker alembic alembic.ini`.

**Commands.** `cp .env.example .env && docker compose up --build` · `docker compose ps` · `docker compose logs -f celery_worker`.

**Expected output.** `migrate` exits 0; `backend` healthy; `http://localhost:8080` serves the app; full alarm flow works inside Compose.

**How to test.** `docker compose down -v && up` → clean DB migrated. `docker compose restart backend` → WS reconnects. `docker compose stop celery_worker`, fire an alarm, `start` → it fires late with the original `trigger_at`.

**Common errors.** `connection refused postgres:5432` → `depends_on` without `condition`. nginx `502` on `/ws` → missing Upgrade headers. `alembic` can't find `app` in the migrate container → `WORKDIR /app` + `PYTHONPATH`.

**Security considerations.** No host port for postgres/redis in the prod compose (only in override); non-root containers; secrets via `.env` not baked into images.

**Completion checklist.**
- [ ] One command boots everything; migrations applied by `migrate`
- [ ] All inter-service URLs use service names
- [ ] Health checks + ordering verified by `compose ps`
- [ ] End-to-end alarm flow passes inside Compose

---

## PHASE 17 — Testing

**Goal.** Automated coverage for auth, tasks, alarms, authorization, worker, and WebSocket.

**Architecture changes.** `tests/conftest.py` with a dedicated `task_tracker_test` database, dependency override for `get_db`, truncate-after-each-test, `AsyncClient(transport=ASGITransport(app))`, and a fake publisher for the worker.

**Files to create.** `tests/conftest.py`, `tests/test_auth.py`, `tests/test_tasks.py`, `tests/test_alarms.py`, `tests/test_authorization.py`, `tests/test_worker.py`, `tests/test_websocket.py`, `tests/test_migrations.py`, `pytest.ini` (`asyncio_mode = auto`).

**Files to modify.** `requirements-dev.txt` (`pytest`, `pytest-asyncio`, `httpx`, `fakeredis`).

**Database changes.** Test DB created by a session fixture (`CREATE DATABASE task_tracker_test` via the sync driver), tables via `Base.metadata.create_all` for speed; one test runs `alembic upgrade head` on an empty DB to verify the migration chain.

**API endpoints.** None.

**Implementation steps / test matrix.**

| Area | Tests |
|---|---|
| Auth | register 201; duplicate 409; login 200; wrong password 401; expired token 401; tampered token 401; `/me` without header 401 |
| Tasks | create, list (+filter, +ordering), get, update, delete (then 404), complete; naive datetime 422; `user_id` in body 422 |
| Authorization | user B gets 404 on A's task (GET/PUT/DELETE/complete) and A's alarm (PUT/DELETE/ack) |
| Alarms | create 201, past time 422, update, update-after-trigger 409, delete, missed/ack cycle |
| Worker | due alarm claimed + event built; already-triggered ignored; completed task ignored; batch limit honoured; publisher called once per alarm **after** commit (fake publisher asserts `is_triggered` is already committed via a second session) |
| WebSocket | no auth → 4401; bad token → 4401; valid → `auth_ok`; `manager.send_to_user` delivers to the right user only (direct unit test of the manager + TestClient `websocket_connect`) |
| Migrations | `alembic upgrade head` from empty DB; `alembic check` clean |

**Commands.** `pytest -q` · `pytest tests/test_worker.py -k duplicate -vv`.

**Expected output.** `… passed in Ns`, zero warnings about unclosed sessions.

**How to test.** Break ownership on purpose (remove the `Task.user_id == user_id` filter) → authorization tests fail — proves they guard what they claim.

**Common errors.** `RuntimeError: Task attached to a different loop` → `asyncio_mode=auto` + session-scoped engine created inside the loop. Tests leaking data → truncate fixture not `autouse`. WebSocket TestClient hangs → server waits on `receive_json`; always send auth or expect close.

**Security considerations.** Tests prove isolation and token validation — keep them in CI as the regression gate.

**Completion checklist.**
- [ ] Full matrix green locally and in `docker compose run backend pytest`
- [ ] Worker idempotency and ownership tests present and meaningful

---

## PHASE 18 — Security hardening

**Goal.** Apply the baseline production security controls and document which are dev-only.

**Architecture changes.** `slowapi` limiter on login; `/docs` disabled in production; security headers at nginx; env validation at startup.

**Files to create.** `app/core/ratelimit.py`; `SECURITY.md`.

**Files to modify.** `routers/auth.py` (login `@limiter.limit("5/minute")`), `main.py` (`docs_url=None` when production, HSTS/CSP handled in nginx), `nginx.conf`, `config.py` (`JWT_SECRET` must be ≥32 chars when `ENVIRONMENT=production`; refuse to boot otherwise).

**Database changes / API endpoints.** None.

| Measure | Status | Dev-only / Production |
|---|---|---|
| bcrypt password hashing | done (Phase 3) | both |
| JWT expiry (60 min) + `type` claim | done | both; production: shorter access token + refresh token (postponed) |
| Secrets via `.env` / env vars | done | dev: `.env` file; production: secret manager / CI secrets, rotated |
| Pydantic validation + `extra="forbid"` | done | both |
| Ownership checks (404 on cross-user) | done | both |
| SQL injection protection | SQLAlchemy parameterised queries everywhere; never string-format SQL | both |
| CORS | allow `localhost:5173` | dev-only; production is same-origin via nginx → CORS list empty or exact domain |
| Safe error responses | done (Phase 2) | both |
| Password hash never serialised | done | both |
| WebSocket auth | first-message JWT | both; production upgrade: one-time ticket |
| Rate limiting on login | **Phase 18** (`slowapi`, 5/min/IP) | production required; dev optional |
| HTTPS | **recommend** TLS termination at nginx/Caddy or the cloud LB; `wss://` automatically | production required |
| `/docs` exposure | disabled when `ENVIRONMENT=production` | production |
| Redis/Postgres network exposure | no host ports; `requirepass` on Redis | production |
| Token storage in browser | `localStorage` | dev acceptable; production: `httpOnly` + `SameSite=Strict` cookie (with CSRF token) |
| Security headers (HSTS, CSP, X-Content-Type-Options) | nginx | production |
| Dependency pinning + `pip-audit`/`npm audit` | CI | production |

**Commands.** `pip install slowapi pip-audit && pip-audit` · `npm audit` · `curl -i -X POST localhost:8000/api/auth/login …` ×6 → 429 on the sixth.

**Expected output.** 6th login attempt within a minute → `429 {"detail":"Rate limit exceeded"}`; `ENVIRONMENT=production` without a strong secret → startup refuses with a clear message.

**How to test.** Run the Phase 17 suite; add tests for 429 and for `/docs` being 404 in production mode.

**Common errors.** `slowapi` needs `request: Request` in the route signature. Rate limiter behind a proxy counts the proxy IP → configure `X-Forwarded-For` trust in nginx + `uvicorn --proxy-headers`.

**Security considerations.** This phase *is* the list; keep `SECURITY.md` as the living checklist.

**Completion checklist.**
- [ ] Login rate limit + production guards in place
- [ ] `SECURITY.md` lists every control with dev/prod status
- [ ] Audit tools run clean

---

## PHASE 19 — Production-readiness review

**Goal.** Verify the system is operable: observability, resilience, deployment hygiene.

**Architecture changes.** None required; small additions.

**Files to create.** `README.md` (run/dev/test/deploy), `.github/workflows/ci.yml` (ruff + pytest + `npm run build` + `docker compose build`), `backend/app/routers/health.py` (`/api/health/ready` checks DB + Redis; `/api/health/live` is trivial).

**Files to modify.** Compose (restart policies `unless-stopped`, resource limits, log rotation), Dockerfiles (pinned base images, non-root).

**Review checklist (each item = pass/fail with a note).**
- [ ] `docker compose up` from a clean clone works with only `.env` filled
- [ ] Migrations run by the `migrate` service; `alembic check` clean in CI
- [ ] Exactly one `celery_beat` replica; worker concurrency sized; `expires` on the tick task
- [ ] Host clock synced (NTP) on every node — alarms depend on it
- [ ] Structured logs with timestamps + request path; worker logs claim counts; WS logs connect/disconnect per user
- [ ] `/health/live` + `/health/ready` wired to orchestrator health checks
- [ ] Graceful shutdown: uvicorn drains, subscriber task cancelled, Redis client closed
- [ ] Postgres volume backed up; connection pool sizes set (`pool_size`, `max_overflow`)
- [ ] `uvicorn --workers N` (or multiple backend replicas) tested: events still reach users (Pub/Sub per process)
- [ ] TLS in front; `wss://` verified from the browser
- [ ] Error budget decisions written down: alarm latency ≤ interval + 5 s; missed-alarm reconcile on reconnect
- [ ] Postponed features listed in README: recurring alarms, refresh tokens, WS tickets, notifications table, push/email channels, per-user timezone, outbox for exactly-once publish

**Commands.** `docker compose -f docker-compose.yml up --build -d && docker compose ps` · `ab`/`hey` smoke on `/api/tasks` · `docker compose logs --since 10m`.

**Expected output.** All services `healthy`; p95 of `/api/tasks` < 100 ms locally; alarm fires within one interval.

**How to test.** Chaos checklist: kill worker / kill Redis / kill backend one at a time while an alarm is pending; confirm each recovers and the alarm is either delivered or shown as missed.

**Common errors.** Clock skew between containers (rare with Docker Desktop, real on VMs). Forgetting `proxy_read_timeout` → WS drops every 60 s behind nginx.

**Security considerations.** Final pass over `SECURITY.md`; rotate `JWT_SECRET` before first real deploy.

**Completion checklist.** All review items pass or have a documented, accepted exception.

---

## 4. Exact Phase 2 starting point

**Pre-conditions:** Phase 1 checklist green (models corrected, async `env.py`, single head), Postgres container running, `backend/.venv` active.

**Create, in this order (each explained before it is written):**

1. `app/core/config.py` — `Settings` + `get_settings()` (fields listed in Phase 2 step 1).
2. `app/core/database.py` — engine, `AsyncSessionLocal`, `get_db`.
3. `app/core/exceptions.py` — `AppError` family + `register_exception_handlers(app)`.
4. `app/core/logging.py` — `configure_logging()`.
5. `app/schemas/common.py` — `ErrorResponse {detail, code}`.
6. `app/routers/health.py` — `GET /health`.
7. `app/main.py` — `create_app()`, `lifespan`, CORS, handlers, routers under `/api`.
8. `.env` (copied from `.env.example`), `requirements.txt`.

**Commands:**
```bash
cd ~/projects/task_alarm/backend && source .venv/bin/activate
pip install "fastapi>=0.115" "uvicorn[standard]" "sqlalchemy[asyncio]>=2.0" asyncpg alembic "pydantic>=2.7" pydantic-settings email-validator pyjwt bcrypt
pip freeze > requirements.txt
uvicorn app.main:app --reload --port 8000
curl -s localhost:8000/api/health && open http://127.0.0.1:8000/docs
```

**Expected:** startup log without warnings; `{"status":"ok","database":"ok"}`; Swagger with the `health` tag.

## 5. Phase 2 completion checklist

- [ ] `Settings` loads from `.env`; missing `JWT_SECRET`/`DATABASE_URL` fails fast with a clear error
- [ ] `get_db` yields an `AsyncSession` with `expire_on_commit=False`; `/api/health` executes `SELECT 1`
- [ ] Error handlers: `AppError` → JSON envelope with the right status; 422 validation envelope; unexpected exception → generic 500 (traceback only in logs)
- [ ] CORS allows `http://localhost:5173`
- [ ] `/docs` renders; `openapi.json` has `title`, `version`, tags
- [ ] `requirements.txt` committed; `.env` ignored; `.env.example` complete
- [ ] You can explain, in your own words: why `expire_on_commit=False`, why the session is a dependency, why services (not routers) will own `commit()`

---

## 6. Verification map (how the whole thing is proven end-to-end)

| Layer | Proof |
|---|---|
| API | Swagger 11-step flow (Phase 6) + pytest matrix (Phase 17) |
| Ownership | `test_authorization.py` + manual user-B checks in Phases 4/5 |
| Worker idempotency | concurrency test (Phase 10) + `test_worker.py` |
| Real-time path | `redis-cli SUBSCRIBE` (12) → `python -m websockets` (13) → dashboard "Live" (14) |
| Offline/missed | close tab, fire alarm, reopen → banner (14); `/alarms/missed` + `/ack` tests (17) |
| Browser policies | deny permission / no-click page / two tabs (15) |
| Deployment | `compose down -v && up` + chaos checklist (16, 19) |

---

## 7. Postponed (explicitly out of MVP scope)

Recurring alarms · refresh tokens / httpOnly cookie auth · one-time WS tickets · `notifications` table + email/push channels · per-user timezone column · outbox table for exactly-once publish · Redis Streams instead of Pub/Sub · task descriptions/priorities/tags · multi-alarm UI · Flower dashboard.
