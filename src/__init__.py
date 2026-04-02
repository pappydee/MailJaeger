"""MailJaeger - Local AI email processing system"""

from src.version import VERSION

__version__ = VERSION
__author__ = "MailJaeger Team"
__description__ = "Fully local, privacy-first AI email processing system"

CHANGELOG = [
    {
        "version": "1.1.0",
        "date": "2026-02-25",
        "changes": [
            "Non-blocking processing trigger: /api/processing/trigger returns run_id immediately",
            "Real-time progress tracking: processed/total/spam/action_required counts in /api/status",
            "AI stability: configurable timeout (default 30s), Ollama num_ctx/num_predict/temperature options",
            "Improved AI JSON parsing: strip code fences, validate required fields, clamp enum values",
            "HTML always stripped from email content; content capped at 1500 chars",
            "Responsive mobile-first UI: flex-wrap header, stacked filters, touch-friendly buttons",
            "Visible error banners/toasts for all API failures",
            "SAFE MODE badge in UI header",
            "Spinner on Process Now button during active run; auto-stop polling on completion",
            "Fix /api/processing/trigger: accept optional JSON body (no 422 when body omitted)",
            "Add ClassificationOverride table for application-level learning",
            "Add overridden/original_classification/override_rule_id fields to ProcessedEmail",
            "New endpoint POST /api/emails/{id}/override: update classification + store rule",
            "EmailProcessor applies override rules before AI (skip AI when rule matches)",
            "LEARNING_ENABLED config flag controls whether override rules are persisted",
            "EmailDetailResponse extended with overridden, original_classification, override_rule_id",
        ],
    },
    {
        "version": "1.0.1",
        "date": "2026-02-24",
        "changes": [
            "Browser-based API key login (no header extensions required)",
            "Session cookie authentication (HttpOnly, SameSite=Lax)",
            "GET /api/status endpoint: real-time job status and progress",
            "GET /api/version endpoint: version info and changelog",
            "Progress bar and current-task indicator in UI",
            "Version history modal in UI",
            "Logout button in header",
        ],
    },
    {
        "version": "1.0.0",
        "date": "2025-01-01",
        "changes": [
            "Initial release",
            "Bearer token API authentication",
            "Email processing dashboard",
            "IMAP integration with AI classification",
            "Pending actions with two-step approval",
            "Rate limiting and security headers",
        ],
    },
]
