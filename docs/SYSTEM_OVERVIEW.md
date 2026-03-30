# MailJaeger — System Overview

> Consolidated handover document for continuation of development.
> Last verified against codebase: 2026-03-29

---

## 1. Vision and Core Objective

MailJaeger is a fully local, privacy-preserving, AI-driven email management system
designed to:

- Autonomously process **all** emails (not only unread)
- Classify, prioritize, and organize emails based on learned user behavior
- Extract actionable information (tasks, deadlines, required replies)
- Provide a daily operational summary
- Support semi-automated or fully automated execution of actions
- Continuously learn from user decisions and historical data

**Key principle:**
Local-first, DSGVO-compliant, no external data leakage, full user control.

---

## 2. Architecture Overview

### 2.1 High-Level Data Flow

```
IMAP Mailbox
     │
     ▼
┌──────────────┐
│  Ingestion   │  Phase 1 — fast, no AI
│  (IMAP pull) │  Dedup via body_hash, thread_id reconstruction
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Analysis   │  Phase 2 — AI-heavy
│  (LLM batch) │  Classification, priority, spam, task extraction
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Actions    │  Phase 3 — controlled execution
│  (queue)     │  proposed → approved → executed
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Learning   │  Continuous feedback loop
│  (patterns)  │  Folder patterns, decision events
└──────────────┘
```

### 2.2 Component Map

| Component | Location | Purpose |
|-----------|----------|---------|
| **FastAPI application** | `src/main.py` | 33 API endpoints, frontend serving, background tasks |
| **Email processor** | `src/services/email_processor.py` | Two-phase orchestrator (ingestion + analysis) |
| **Mail ingestion** | `src/services/mail_ingestion_service.py` | IMAP pull, dedup, thread_id resolution |
| **Analysis pipeline** | `src/services/analysis_pipeline.py` | Multi-stage AI analysis |
| **AI service** | `src/services/ai_service.py` | LLM interaction (single + batch), prompt engineering |
| **Action executor** | `src/services/action_executor.py` | Safe IMAP action execution |
| **Thread aggregator** | `src/services/thread_aggregator.py` | Thread state inference, importance scoring |
| **Thread context** | `src/services/thread_context.py` | Thread state persistence |
| **Thread summary** | `src/services/thread_summary_service.py` | Thread-level summaries |
| **Learning service** | `src/services/learning_service.py` | Folder pattern learning from sender domains |
| **Scheduler** | `src/services/scheduler.py` | APScheduler-based daily cron |
| **Search service** | `src/services/search_service.py` | Whoosh full-text search |
| **IMAP service** | `src/services/imap_service.py` | Low-level IMAP operations |
| **Database models** | `src/models/database.py` | SQLAlchemy ORM (13 models) |
| **API schemas** | `src/models/schemas.py` | Pydantic request/response models |
| **Configuration** | `src/config.py` | Settings with validation and fail-closed safety |
| **Frontend** | `frontend/` | Static web dashboard (HTML/JS/CSS) |

### 2.3 Technology Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.11, FastAPI, SQLAlchemy, APScheduler |
| Database | SQLite (file-based, `data/mailjaeger.db`) |
| AI | Ollama (local), default model `qwen2.5:7b` |
| Search | Whoosh full-text indexing |
| Frontend | Vanilla HTML/JS/CSS (no framework) |
| Deployment | Docker Compose, systemd service, Raspberry Pi 5 |

---

## 3. Data Model

### 3.1 Core Models

**ProcessedEmail** — primary entity (13+ relationships)

| Field Group | Fields |
|-------------|--------|
| Identity | `id`, `message_id`, `uid`, `imap_uid` |
| Threading | `thread_id`, `thread_state`, `thread_priority`, `thread_importance_score` |
| Headers | `subject`, `sender`, `recipients`, `date`, `received_at`, `folder` |
| Content | `body_plain`, `body_html`, `snippet`, `body_hash` |
| AI analysis | `summary`, `category`, `spam_probability`, `action_required`, `priority`, `suggested_folder`, `reasoning` |
| Pipeline | `analysis_state`, `analysis_version`, `importance_score` |
| Override | `overridden`, `override_rule_id`, `original_classification` |
| Status | `is_spam`, `is_processed`, `is_archived`, `is_flagged`, `is_resolved` |

**ActionQueue** — proposed/approved/executed action items

**PendingAction** — legacy IMAP action approval workflow

**DecisionEvent** — records every classification decision with source, confidence, and user confirmation

**DailyReport** — cached daily report with generation status (`pending` / `running` / `ready` / `failed`)

**Other models:** `EmailTask`, `ProcessingRun`, `LearningSignal`, `FolderPattern`,
`ApplyToken`, `ClassificationOverride`, `AppSetting`, `AuditLog`, `AnalysisProgress`

### 3.2 Key Data Flows

- **Deduplication:** `body_hash` (SHA-256 of normalized body) prevents re-analysis of identical content
- **Thread reconstruction:** `In-Reply-To` / `References` headers → lookup parent in DB → assign shared `thread_id`
- **Importance scoring:** 0–100 composite of recency, urgency keywords, thread participation, sender domain reputation
- **Analysis versioning:** `analysis_version` tracks pipeline version; enables re-analysis when pipeline changes

---

## 4. Current Implementation Status

### 4.1 Fully Implemented ✅

| Feature | Details |
|---------|---------|
| **IMAP ingestion** | Two-phase: fast ingest (Phase 1) + AI analysis (Phase 2); UID-based checkpointing per folder |
| **AI classification** | Single + batch LLM calls via Ollama; category, priority, spam, action_required, task extraction |
| **Batch processing** | `analyze_emails_batch()` — single LLM call for multiple emails (configurable `AI_BATCH_SIZE`, default 10) |
| **Thread system** | `thread_id` reconstruction from `In-Reply-To`/`References`; state inference (`waiting_for_me`, `waiting_for_other`, `in_conversation`, `resolved`, `informational`, `auto_generated`); thread importance scoring |
| **body_hash dedup** | SHA-256 body hash; reuses analysis from previously seen identical content |
| **Decision events** | Full model with event_type, source, confidence, user_confirmed; used in reports and action queue |
| **Importance score** | Email-level 0–100 composite score (recency, urgency keywords, thread participation, domain reputation) |
| **Analysis versioning** | `analysis_version` on every email; currently `PIPELINE_VERSION = "1.0.0"` |
| **Action system** | `ActionQueue` with status pipeline: `proposed → approved → executed → failed`; `PendingAction` for IMAP ops |
| **Safe mode** | `safe_mode=true` by default; persisted in `AppSetting`; fail-closed for web-exposed deployments |
| **Approval workflow** | Preview → approve/reject → apply with time-limited `ApplyToken` |
| **Daily report** | Async background generation; structured response with important/action/unresolved/spam items, suggested actions, thread context |
| **Scheduler** | APScheduler cron trigger; default 02:00 Europe/Berlin; manual trigger via API; cancellation support |
| **Search** | Whoosh full-text + semantic search with filters |
| **Security** | Multi-key API auth, rate limiting, CORS, credential protection, log redaction, session-only tokens, host allowlist |
| **Audit logging** | All processing actions logged; structured timestamped output |
| **Docker deployment** | `docker-compose.yml` + `docker-compose.prod.yml`; optional Ollama sidecar; TLS cert volume |
| **API** | 33 endpoints covering emails, processing, actions, reports, settings, health |
| **Frontend** | Web dashboard with email list, detail view, search, health monitoring, action management |

### 4.2 Partially Implemented ⚠️

**Learning system**
- ✅ Folder pattern learning from sender domains works
- ✅ `LearningSignal` + `FolderPattern` models populated
- ❌ `detect_folder_movements()` is a placeholder
- ❌ No automatic folder-scan-based learning yet
- ❌ Tracks only sender domain patterns, not subject or content patterns

**Classification override**
- ✅ Users can override AI classification via API
- ✅ Override creates `ClassificationOverride` rule
- ❌ Override rules are not automatically re-applied to future emails from same sender/pattern

**UI**
- ✅ Functional dashboard with email list, search, health status
- ❌ No real-time progress visualization
- ❌ No interactive action execution buttons in email detail view
- ❌ Needs UX polish

**Scheduling control**
- ✅ Schedule time configurable via `POST /api/settings`
- ❌ No enable/disable toggle exposed in UI
- ❌ No recurring schedule editor

### 4.3 Not Yet Implemented ❌

| Feature | Description |
|---------|-------------|
| **Sender profiling** | No `SenderProfile` model; no sender behavior modeling beyond domain-level pattern matching |
| **Full folder-based learning** | No periodic IMAP folder scan to learn from user's manual folder moves |
| **Thread-level summaries** | `thread_summary_service.py` exists but thread summaries are not surfaced in daily reports or UI |
| **Automated action re-application** | Classification overrides don't automatically apply to future matching emails |
| **Guided installer** | Setup requires Docker knowledge and manual `.env` configuration |
| **macOS menu bar / iOS client** | Future platform targets, not started |

---

## 5. API Reference

### 5.1 Endpoint Summary

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| GET | `/` | Global | Serve frontend dashboard |
| POST | `/api/auth/login` | — | Exchange API key for session cookie |
| POST | `/api/auth/logout` | — | Invalidate session |
| GET | `/api/auth/verify` | — | Check authentication status |
| GET | `/api/version` | — | Version info and changelog |
| GET | `/api/health` | — | Unauthenticated health check |
| GET | `/api/status` | ✅ | Real-time processing status |
| GET | `/api/dashboard` | ✅ | Dashboard overview with health checks |
| POST | `/api/emails/search` | ✅ | Full-text/semantic email search |
| POST | `/api/emails/list` | ✅ | List emails with filters and sorting |
| GET | `/api/emails/{id}` | ✅ | Get email details |
| POST | `/api/emails/{id}/resolve` | ✅ | Mark email resolved/unresolved |
| POST | `/api/emails/{id}/override` | ✅ | Override AI classification |
| POST | `/api/processing/trigger` | ✅ | Manually trigger processing |
| POST | `/api/processing/cancel` | ✅ | Cancel active processing run |
| GET | `/api/processing/runs` | ✅ | Processing run history |
| GET | `/api/processing/runs/{id}` | ✅ | Specific processing run |
| GET | `/api/settings` | ✅ | Current settings (sanitized) |
| POST | `/api/settings` | ✅ | Update settings |
| GET | `/api/folders` | ✅ | List IMAP folders (cached) |
| GET | `/api/actions` | ✅ | Action queue items with thread context |
| POST | `/api/reports/daily/suggested-actions` | ✅ | Queue suggested action from daily report |
| POST | `/api/reports/daily/events` | ✅ | Report decision event |
| POST | `/api/actions/{id}/approve` | ✅ | Approve action |
| POST | `/api/actions/{id}/reject` | ✅ | Reject action |
| POST | `/api/actions/{id}/execute` | ✅ | Execute action |
| GET | `/api/pending-actions` | ✅ | Legacy pending IMAP actions |
| POST | `/api/pending-actions/preview` | ✅ | Preview before applying |
| GET | `/api/pending-actions/{id}` | ✅ | Specific pending action |
| POST | `/api/pending-actions/{id}/approve` | ✅ | Approve pending action |
| POST | `/api/pending-actions/apply` | ✅ | Apply approved actions (with token) |
| POST | `/api/pending-actions/{id}/apply` | ✅ | Apply single action |
| GET | `/api/reports/daily` | ✅ | Daily report (async envelope) |

### 5.2 Key Flows

**Processing trigger:**
```
POST /api/processing/trigger → background task → scheduler.trigger_manual_run_async()
GET  /api/status              → poll for progress (phase, percentage, counts)
POST /api/processing/cancel   → request cancellation
```

**Action approval:**
```
GET  /api/actions                        → list proposed/approved actions
POST /api/actions/{id}/approve           → approve action
POST /api/actions/{id}/execute           → execute approved action
```

**Daily report:**
```
GET  /api/reports/daily                  → returns {status: "pending"} first time
                                         → triggers background generation
GET  /api/reports/daily                  → poll until {status: "ready", report: {...}}
POST /api/reports/daily/suggested-actions → queue suggestion from report
```

---

## 6. Configuration

All configuration is via environment variables (`.env` file). Key groups:

| Group | Key Variables | Defaults |
|-------|---------------|----------|
| **Security** | `API_KEY`, `API_KEY_FILE`, `ALLOWED_HOSTS`, `CORS_ORIGINS` | localhost-only, safe mode on |
| **IMAP** | `IMAP_HOST`, `IMAP_PORT`, `IMAP_USERNAME`, `IMAP_PASSWORD` | Port 993, SSL on |
| **AI** | `AI_ENDPOINT`, `AI_MODEL`, `AI_BATCH_SIZE`, `AI_TIMEOUT` | `http://host.docker.internal:11434`, `qwen2.5:7b`, batch 10 |
| **Processing** | `MAX_EMAILS_PER_RUN`, `SPAM_THRESHOLD` | 200 emails, 0.7 threshold |
| **Schedule** | `SCHEDULE_TIME`, `SCHEDULE_TIMEZONE` | 02:00, Europe/Berlin |
| **Safety** | `SAFE_MODE`, `REQUIRE_APPROVAL` | Both `true` |
| **Storage** | `DATABASE_URL`, `STORE_EMAIL_BODY` | `./data/mailjaeger.db`, body NOT stored |

See `.env.example` for the full list.

---

## 7. Deployment

### 7.1 Docker Compose (Recommended)

```bash
cp .env.example .env
# Edit .env with your IMAP credentials and API key
docker compose up -d
```

- `docker-compose.yml` — development (includes Ollama sidecar)
- `docker-compose.prod.yml` — production (expects external Ollama, uses `host.docker.internal`)

### 7.2 Bare Metal

```bash
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
ollama serve &
python -m uvicorn src.main:app --host 127.0.0.1 --port 8000
```

### 7.3 Systemd Service

`mailjaeger.service` is provided for persistent deployment on Linux / Raspberry Pi.

---

## 8. Safety Model

MailJaeger implements defense-in-depth safety:

1. **`safe_mode` (default: on)** — prevents all destructive IMAP actions (move, delete, mark-read)
2. **`require_approval` (default: on)** — every action must be manually approved before execution
3. **Folder allowlist** — AI can only suggest pre-approved folders
4. **Time-limited apply tokens** — `ApplyToken` with expiration prevents replay attacks
5. **Audit trail** — every action logged in `AuditLog` with timestamp and context
6. **Fail-closed** — web-exposed deployments must have `safe_mode` or `require_approval` enabled

---

## 9. Current Limitations

### 9.1 Learning System (Major Gap)

The learning service records folder patterns from sender domains but does not yet:
- Periodically scan IMAP folders to learn from user's manual moves
- Build content/subject-based patterns
- Automatically re-apply learned rules to incoming emails
- Model sender behavior beyond domain-level matching

### 9.2 Performance

- One LLM batch call per `AI_BATCH_SIZE` emails (default 10)
- No streaming or parallel LLM calls
- Raspberry Pi 5 constrains model size and inference speed

### 9.3 UI

- Functional but minimal
- No real-time progress bar (requires polling `/api/status`)
- No interactive action buttons in email detail view
- No dedicated deadlines view or thread view

### 9.4 Automation Control

- Schedule time configurable via API but not exposed in UI
- No enable/disable toggle for the scheduler
- No recurring schedule editor

### 9.5 LLM Reliability

- Inconsistent outputs from local models
- Structured prompts + fallback classification mitigate but don't eliminate the issue
- Umlaut handling problems observed in some model outputs

### 9.6 IMAP Fragility

- Folder naming varies across providers (e.g. "Junk" vs "Spam")
- TLS/certificate issues in some Docker environments
- Connection timeouts on slow networks

---

## 10. High-Priority Next Steps

### 10.1 Learning System (Core Milestone)

1. Implement periodic IMAP folder scanning to detect user moves
2. Build sender models (sender frequency, typical category, response patterns)
3. Learn from subject/content patterns, not just sender domain
4. Auto-apply high-confidence rules to incoming emails
5. Integrate user feedback loop (override → rule → application)

### 10.2 Sender Profiling

1. Create `SenderProfile` model (domain, frequency, category distribution, response rate)
2. Populate from historical email data
3. Use in importance scoring and classification

### 10.3 Thread Summaries in Reports

1. Surface thread-level summaries in daily reports
2. Add thread view to frontend
3. Show thread state progression over time

### 10.4 UI Improvements

1. Real-time progress visualization (WebSocket or SSE)
2. Action buttons in email detail view
3. Scheduler enable/disable toggle
4. Dedicated deadlines and thread views
5. Polished UX with responsive design

### 10.5 Installation & Usability

1. Guided installer script (interactive `.env` generation)
2. Automated TLS certificate handling
3. One-command setup for Raspberry Pi

### 10.6 Performance Optimization

1. Parallel LLM calls for independent emails
2. Streaming responses for faster perceived latency
3. Incremental re-analysis (only re-analyze when `analysis_version` changes)

---

## 11. Strategic Direction

MailJaeger is evolving toward **a local, self-learning email operating system** rather
than a simple classifier.

**Key differentiators:**
- Full local processing — no cloud dependency
- Continuous learning from user behavior
- Explainable decisions (reasoning field, audit trail)
- Controllable automation (safe mode, approval workflow)

**Platform roadmap:**
1. Current: Docker Compose / Raspberry Pi / macOS dev
2. Near-term: Polished web UI, guided installer
3. Future: macOS menu bar app, iOS client (SwiftUI)

---

## 12. Summary Status

| Area | Status |
|------|--------|
| Foundation | ✅ Solid — FastAPI, SQLAlchemy, Docker, 33 API endpoints |
| Core concept | ✅ Well-defined — two-phase pipeline, safety-first |
| Safety model | ✅ Implemented — safe mode, approval, audit trail, fail-closed |
| Thread system | ✅ Implemented — reconstruction, state inference, importance scoring |
| Batch processing | ✅ Implemented — configurable batch size, fallback handling |
| Daily reports | ✅ Implemented — async generation, structured items, suggested actions |
| Learning | ⚠️ Partial — sender domain patterns only; no folder scanning or content patterns |
| Sender profiling | ❌ Not started |
| Scalability | ⚠️ Adequate for typical mailbox; not optimized for very large volumes |
| UX | ⚠️ Early stage — functional dashboard, needs polish |

---

*This document reflects the current system state and intended trajectory.
Use it as a baseline for continued structured development.*
