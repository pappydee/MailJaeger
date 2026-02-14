# Production Deployment Notes

## Database Schema Requirements

### Critical Tables

When deploying MailJaeger in production, ensure the database schema is complete before starting the application.

#### Required Table: pending_actions

The `pending_actions` table **MUST exist** when `REQUIRE_APPROVAL=true` is configured. This table stores email actions that require manual approval before being applied.

**Startup Check:**
- MailJaeger performs a fail-closed startup check to verify the `pending_actions` table exists
- If the table is missing, the application will **exit immediately** with an error message
- This prevents silent failures where actions are queued but the database can't store them

**Error Example:**
```
❌ Startup Error: Database schema incomplete: 'pending_actions' table not found. 
This table is required when REQUIRE_APPROVAL=true. 
Please run database initialization to create all tables.
```

### Database Initialization

MailJaeger uses SQLAlchemy to manage database tables. The schema is automatically created on first startup via `init_db()` in `src/database/connection.py`.

**Automatic Table Creation:**
```python
# On application startup, this is called:
Base.metadata.create_all(bind=engine)
```

This creates all required tables including:
- `processed_emails` - Email analysis results
- `pending_actions` - Actions awaiting approval
- `apply_tokens` - Two-step approval tokens
- `email_tasks` - Extracted tasks
- `processing_runs` - Processing job history
- `learning_signals` - User feedback for ML

**Manual Initialization:**

If you need to manually initialize the database (e.g., in a container that mounts an empty volume):

```python
from src.database.connection import init_db
from src.config import get_settings

# Load settings
settings = get_settings()

# Initialize database (creates all tables)
init_db()
```

Or via Python CLI:
```bash
python -c "from src.database.connection import init_db; init_db()"
```

### Database Migrations

**Current Approach:**
- MailJaeger currently uses SQLAlchemy's `create_all()` for table creation
- No migration framework (like Alembic) is currently integrated
- Schema changes require manual table updates or dropping/recreating the database

**For Production:**
- Consider backing up your database before schema changes
- Use database file path: `sqlite:///./data/mailjaeger.db` (default)
- The database file is created automatically in the `data/` directory

### Troubleshooting

**Error: "pending_actions table not found"**

1. Check if database file exists:
   ```bash
   ls -la data/mailjaeger.db
   ```

2. Verify table exists:
   ```bash
   sqlite3 data/mailjaeger.db ".tables"
   ```

3. If tables are missing, delete the database and let MailJaeger recreate it:
   ```bash
   rm data/mailjaeger.db
   # Restart MailJaeger - tables will be created automatically
   ```

4. For Docker deployments, ensure the data volume is properly mounted and writable

**Error: "Failed to verify database schema"**

This indicates a database connection issue. Check:
- Database file permissions (must be readable/writable)
- Database file directory exists and is writable
- No file system errors or disk space issues
- SQLite file is not corrupted

### Security Considerations

- Database credentials (if using PostgreSQL/MySQL) are sanitized in error logs
- Startup checks run in fail-closed mode (exit on error, don't continue)
- No automatic migrations in production to prevent data loss
- Schema verification happens after `init_db()` but before serving requests

### Related Settings

```bash
# Database configuration
DATABASE_URL=sqlite:///./data/mailjaeger.db

# Approval workflow (requires pending_actions table)
REQUIRE_APPROVAL=false  # Set to true to enable approval workflow

# Debug mode (affects error verbosity)
DEBUG=false  # In production, keep false to avoid credential leakage
```

### See Also

- [README.md](README.md) - General setup and configuration
- [SECURITY.md](SECURITY.md) - Security best practices
- [src/database/connection.py](src/database/connection.py) - Database initialization code
- [src/database/startup_checks.py](src/database/startup_checks.py) - Startup validation logic
