# Changelog

All notable changes to MailJaeger will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.1] - 2026-02-25

### Fixed
- `/api/processing/trigger` now accepts an optional JSON body (no 422 error when body is omitted)

### Added
- `ClassificationOverride` table for application-level learning
- `overridden`, `original_classification`, and `override_rule_id` fields on `ProcessedEmail`
- New endpoint `POST /api/emails/{id}/override`: update classification and store override rule
- `EmailDetailResponse` extended with `overridden`, `original_classification`, `override_rule_id`

### Changed
- `EmailProcessor` applies override rules before AI analysis (skips AI when a rule matches)
- `LEARNING_ENABLED` config flag controls whether override rules are persisted

---

## [1.1.0] - 2026-02-24

### Added
- Non-blocking processing trigger: `/api/processing/trigger` returns `run_id` immediately
- Real-time progress tracking: `processed`/`total`/`spam`/`action_required` counts in `/api/status`
- AI stability: configurable timeout (default 30 s), Ollama `num_ctx`/`num_predict`/`temperature` options
- Improved AI JSON parsing: strip code fences, validate required fields, clamp enum values
- HTML always stripped from email content; content capped at 1500 chars
- Responsive mobile-first UI: flex-wrap header, stacked filters, touch-friendly buttons
- Visible error banners/toasts for all API failures
- SAFE MODE badge in UI header
- Spinner on "Process Now" button during active run; auto-stop polling on completion

---

## [1.0.1] - 2026-02-24

### Added
- Browser-based API key login (no browser extension required)
- Session cookie authentication (HttpOnly, SameSite=Lax)
- `GET /api/status` endpoint: real-time job status and progress
- `GET /api/version` endpoint: version info and changelog
- Progress bar and current-task indicator in UI
- Version history modal in UI
- Logout button in header

---

## [1.0.0] - 2025-01-01

### Added - Initial Release

#### Core Features
- Complete IMAP email retrieval system
- AI-powered email analysis using local LLM (Ollama)
- Spam classification with AI + heuristic approach
- Automatic priority assignment (HIGH/MEDIUM/LOW)
- Task extraction with due dates and confidence scores
- Automated mailbox operations (archive, flag, spam folder)
- Learning system with pattern recognition
- Full-text search with Whoosh indexing
- RESTful API with FastAPI
- Daily scheduling system (08:00 Europe/Berlin)
- Complete audit trail and logging

#### Infrastructure
- SQLite database with SQLAlchemy ORM
- Docker and Docker Compose support
- Systemd service configuration
- Automated installation script
- CLI management tool
- Comprehensive error handling and recovery

#### Security
- 100% local operation (no cloud services)
- No telemetry or external data transmission
- Secure credential handling
- Complete data sovereignty

---

[1.1.1]: https://github.com/pappydee/MailJaeger/releases/tag/v1.1.1
[1.1.0]: https://github.com/pappydee/MailJaeger/releases/tag/v1.1.0
[1.0.1]: https://github.com/pappydee/MailJaeger/releases/tag/v1.0.1
[1.0.0]: https://github.com/pappydee/MailJaeger/releases/tag/v1.0.0
