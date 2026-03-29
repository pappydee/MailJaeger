# MailJaeger — Architecture Reference

> Technical architecture for developers continuing work on MailJaeger.

---

## 1. System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        MailJaeger System                            │
│                                                                     │
│  ┌──────────┐    ┌──────────────┐    ┌────────────┐               │
│  │ Frontend │◄──►│   FastAPI     │◄──►│  SQLite DB │               │
│  │ (static) │    │  (src/main)   │    │  (data/)   │               │
│  └──────────┘    └──────┬───────┘    └────────────┘               │
│                         │                                           │
│              ┌──────────┼──────────────┐                           │
│              │          │              │                             │
│              ▼          ▼              ▼                             │
│  ┌───────────────┐ ┌─────────┐ ┌──────────────┐                   │
│  │ EmailProcessor│ │Scheduler│ │ SearchService│                    │
│  │  (orchestrator)│ │(APSched)│ │   (Whoosh)   │                   │
│  └───────┬───────┘ └─────────┘ └──────────────┘                   │
│          │                                                          │
│    ┌─────┴──────┐                                                  │
│    │            │                                                    │
│    ▼            ▼                                                    │
│  Phase 1     Phase 2                                                │
│  ┌────────┐  ┌──────────────┐                                      │
│  │Ingest  │  │AnalysisPipe  │                                      │
│  │Service │  │   + AIService │──────► Ollama (LLM)                 │
│  └───┬────┘  └──────┬───────┘                                      │
│      │              │                                               │
│      ▼              ▼                                               │
│  ┌────────┐  ┌──────────────┐  ┌──────────────┐                   │
│  │ IMAP   │  │ ThreadAggr   │  │ActionExecutor│                    │
│  │Service │  │ + ThreadCtx  │  │              │                    │
│  └───┬────┘  └──────────────┘  └──────┬───────┘                   │
│      │                                │                             │
│      ▼                                ▼                             │
│  ┌──────────────────────────────────────┐                          │
│  │           IMAP Mailbox               │                          │
│  └──────────────────────────────────────┘                          │
│                                                                     │
│  ┌──────────────┐                                                  │
│  │LearningServ  │ ◄── folder patterns, signals                    │
│  └──────────────┘                                                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Processing Pipeline

### Phase 1: Ingestion (No AI)

```
Scheduler / Manual Trigger
        │
        ▼
EmailProcessor._run_ingestion()
        │
        ├─► IMAPService.connect()
        ├─► Fetch UIDs > last stored imap_uid per folder
        ├─► For each new email:
        │     ├─ Parse headers, body, attachments
        │     ├─ Compute body_hash (SHA-256 normalized)
        │     ├─ Resolve thread_id (In-Reply-To / References lookup)
        │     ├─ Check body_hash dedup → reuse existing analysis if found
        │     └─ Insert ProcessedEmail (analysis_state = "ingested")
        └─► Commit batch
```

**Key file:** `src/services/mail_ingestion_service.py`

### Phase 2: Analysis (AI-Heavy)

```
EmailProcessor._run_analysis()
        │
        ├─► Query emails WHERE analysis_state = "ingested"
        ├─► Order by importance_score DESC
        ├─► Batch into groups of AI_BATCH_SIZE (default 10)
        ├─► For each batch:
        │     ├─ AIService.analyze_emails_batch() → single LLM call
        │     ├─ Parse JSON array response
        │     ├─ For each result:
        │     │     ├─ Apply classification (category, priority, spam, etc.)
        │     │     ├─ Extract tasks with due dates
        │     │     ├─ Compute importance_score
        │     │     ├─ Update thread state via ThreadAggregator
        │     │     └─ Create ActionQueue items if action_required
        │     └─ Fallback to _fallback_classification() on LLM failure
        └─► Update analysis_state = "deep_analyzed"
```

**Key files:** `src/services/email_processor.py`, `src/services/ai_service.py`, `src/services/analysis_pipeline.py`

### Phase 3: Action Execution

```
ActionQueue item (status = "proposed")
        │
        ├─► User approves via API → status = "approved"
        │
        ├─► User triggers execute via API
        │     ├─ ActionExecutor validates payload
        │     ├─ Checks safe_mode / require_approval
        │     ├─ Executes IMAP operation (move, mark_read, delete, etc.)
        │     ├─ Logs to AuditLog
        │     └─ Updates status = "executed" or "failed"
        │
        └─► PendingAction path (legacy):
              ├─ Preview → ApplyToken (time-limited)
              ├─ Approve → Apply with token
              └─ Execute IMAP operation
```

**Key file:** `src/services/action_executor.py`

---

## 3. Database Schema (ERD Summary)

```
ProcessedEmail (primary)
  ├── 1:N EmailTask
  ├── 1:N LearningSignal
  ├── 1:N PendingAction
  ├── 1:N DecisionEvent
  ├── 1:N ActionQueue
  └── N:1 ClassificationOverride (via override_rule_id)

ProcessingRun (batch execution record)

FolderPattern (learned folder routing rules)

ApplyToken (time-limited action execution tokens)

AppSetting (key-value runtime configuration)

DailyReport (cached generated reports)

AuditLog (immutable event log)

AnalysisProgress (resumable progress tracking)
```

All models are in `src/models/database.py`. SQLite database at `data/mailjaeger.db`.

---

## 4. Thread Intelligence

### Thread ID Resolution (`mail_ingestion_service.py`)

1. Check `In-Reply-To` header → find parent email in DB → use parent's `thread_id`
2. Parse `References` header → find earliest known message → use its `thread_id`
3. Fallback → generate new `thread_id` from SHA hash of `message_id`

### Thread State Inference (`thread_aggregator.py`)

States: `waiting_for_me`, `waiting_for_other`, `in_conversation`, `resolved`, `informational`, `auto_generated`

Determination based on:
- Whether last email is from user or external sender
- `action_required` flag on latest email
- Newsletter detection
- Thread message count and participant count

### Thread Importance Score (`thread_aggregator.py`)

0.0–100.0 composite:
- `action_required` on any email: +35
- Recent activity (≤24h): +15
- User participation in thread: +10
- Multiple messages: +5–10
- Known important senders: +10
- Newsletter detection: -30

---

## 5. Safety Architecture

```
Request arrives at API
        │
        ▼
┌─────────────────────────┐
│ Authentication middleware│  API key + session cookie
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│ Rate limiting            │  Per-endpoint configurable
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│ Input validation         │  Pydantic models
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│ safe_mode check          │  If on → no destructive IMAP ops
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│ require_approval check   │  If on → must be approved first
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│ Folder allowlist         │  Only pre-approved folders
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│ ApplyToken validation    │  Time-limited, single-use
└────────────┬────────────┘
             │
             ▼
        Execute action
             │
             ▼
        AuditLog entry
```

---

## 6. File Structure

```
MailJaeger/
├── src/
│   ├── main.py                  # FastAPI app, all endpoints, background tasks
│   ├── config.py                # Settings with env vars, validation
│   ├── models/
│   │   ├── database.py          # SQLAlchemy ORM models (13 tables)
│   │   └── schemas.py           # Pydantic request/response schemas
│   └── services/
│       ├── email_processor.py   # Two-phase orchestrator
│       ├── mail_ingestion_service.py  # IMAP ingestion, dedup, thread resolution
│       ├── analysis_pipeline.py # Multi-stage AI analysis
│       ├── ai_service.py        # LLM interaction (single + batch)
│       ├── action_executor.py   # Safe IMAP action execution
│       ├── imap_service.py      # Low-level IMAP operations
│       ├── thread_aggregator.py # Thread state + importance
│       ├── thread_context.py    # Thread state persistence
│       ├── thread_summary_service.py  # Thread summaries
│       ├── learning_service.py  # Folder pattern learning
│       ├── scheduler.py         # APScheduler cron
│       └── search_service.py    # Whoosh full-text search
├── frontend/
│   ├── index.html               # Dashboard HTML
│   ├── app.js                   # Dashboard JavaScript
│   └── styles.css               # Dashboard styles
├── tests/                       # pytest test suite
├── docs/                        # Documentation
├── docker-compose.yml           # Dev compose (with Ollama)
├── docker-compose.prod.yml      # Prod compose (external Ollama)
├── Dockerfile                   # Application container
├── requirements.txt             # Python dependencies
├── requirements-dev.txt         # Dev dependencies (pytest, etc.)
├── .env.example                 # Configuration template
├── cli.py                       # CLI tool
├── install.sh                   # Installation script
├── mailjaeger.service           # systemd unit file
└── setup-security.sh            # Interactive security setup
```

---

## 7. Testing

```bash
# Full test suite
python -m pytest -q

# Specific test files
pytest -q tests/test_action_queue_foundation.py
pytest -q tests/test_v131_docker_consistency.py
pytest -q tests/test_daily_report_cache_and_folder_awareness.py
```

Tests use SQLite in-memory databases and mock external services (IMAP, Ollama).

---

## 8. Development Workflow

### Local Development

```bash
# Setup
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Start Ollama
ollama serve &
ollama pull qwen2.5:7b

# Run application
python -m uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload

# Run tests
python -m pytest -q
```

### Docker Development

```bash
cp .env.example .env
# Edit .env with credentials
docker compose up -d
docker compose logs -f mailjaeger-app
```

---

*For the full system overview including strategic direction and roadmap,
see [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md).*
