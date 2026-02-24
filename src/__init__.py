"""MailJaeger - Local AI email processing system"""

__version__ = "1.0.1"
__author__ = "MailJaeger Team"
__description__ = "Fully local, privacy-first AI email processing system"

CHANGELOG = [
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
