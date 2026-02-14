# MailJaeger

**MailJaeger** is a fully local, privacy-first, **secure-by-default** AI email processing system that autonomously analyzes, structures, prioritizes, archives, and organizes incoming emails. It operates continuously with minimal manual oversight and functions as a structured decision-support layer above a standard IMAP mailbox.

## Key Features

- **🔒 100% Local & Private**: All processing occurs locally, no cloud AI services or telemetry
- **🛡️ Secure by Default**: Token-based authentication, localhost-only binding, safe mode enabled
- **🤖 AI-Powered Analysis**: Automatic email categorization, priority assessment, and task extraction
- **🎯 Smart Filtering**: Intelligent spam detection and action-required identification  
- **📊 Structured Organization**: Automatic archiving with learned folder suggestions
- **🧠 Continuous Learning**: Adapts to your behavior and improves over time
- **🔍 Powerful Search**: Full-text and semantic search with filtering
- **📅 Automated Processing**: Scheduled daily runs with manual trigger option
- **🌐 Web Dashboard**: Modern web interface for email management and monitoring
- **🛠️ RESTful API**: Complete API for custom integrations

## 🔐 Security Features

MailJaeger is designed with security as a core principle:

**Authentication & Authorization:**
- ✅ **Multi-Key API Authentication**: Support for multiple API keys with constant-time comparison
- ✅ **Key Rotation**: Add/remove keys without downtime via comma-separated or file-based config
- ✅ **Protected Routes**: All API and frontend routes require authentication by default
- ✅ **Rate Limiting**: Configurable rate limits on login, API calls, and expensive operations

**Network & Transport Security:**
- ✅ **Localhost Binding**: Server binds to 127.0.0.1 by default (not publicly accessible)
- ✅ **Reverse Proxy Ready**: Full support for X-Forwarded-* headers with TRUST_PROXY setting
- ✅ **Security Headers**: HSTS, X-Content-Type-Options, X-Frame-Options, CSP, Referrer-Policy
- ✅ **Restrictive CORS**: No wildcard origins, explicit allowlist configuration

**Data Protection:**
- ✅ **Credential Protection**: Passwords, tokens, and API keys never logged or exposed
- ✅ **Log Redaction**: Multi-layer sensitive data filtering in all log output
- ✅ **Data Minimization**: Email bodies NOT stored by default (privacy-first)
- ✅ **Docker Secrets**: Support for Docker secrets and file-based credential management

**Application Security:**
- ✅ **Input Validation**: Strict validation and sanitization of all AI outputs
- ✅ **Safe Mode**: Dry-run mode prevents destructive IMAP actions by default
- ✅ **Folder Allowlist**: AI can only suggest pre-approved folders (prevents prompt injection)
- ✅ **Quarantine Folder**: Suspected spam goes to quarantine, not deleted
- ✅ **Request Size Limits**: 10MB default limit prevents large payload attacks
- ✅ **Error Sanitization**: Internal errors never exposed to API responses
- ✅ **Session-Only Storage**: Frontend uses sessionStorage (not localStorage) for tokens

**Monitoring & Auditability:**
- ✅ **Audit Logging**: All email processing actions logged with safe mode status
- ✅ **Structured Logs**: Timestamped, leveled logs with automatic redaction
- ✅ **Health Endpoint**: Unauthenticated health check for monitoring systems
- ✅ **Failed Auth Tracking**: All failed authentication attempts logged with source IP

## System Requirements

### Minimum Requirements
- **Raspberry Pi 5** (16GB RAM recommended) or equivalent Linux system
- **4GB free disk space** (more if storing email bodies and attachments)
- **IMAP email account** (Gmail, Outlook, or any IMAP-compatible service)

### Recommended for Raspberry Pi 5
- Use **Mistral 7B Q4** (4GB RAM) or **Phi-3-mini** (2-3GB RAM) for optimal performance
- Install on SSD for better performance than SD card

## Quick Start

### 1. Install System Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11 and dependencies
sudo apt install -y python3.11 python3.11-venv python3-pip git

# Install Ollama (local LLM server)
curl -fsSL https://ollama.com/install.sh | sh
```

### 2. Clone and Setup

```bash
# Clone repository
git clone https://github.com/pappydee/MailJaeger.git
cd MailJaeger

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure Local LLM

```bash
# Pull AI model (choose one)
ollama pull mistral:7b-instruct-q4_0  # Recommended for Raspberry Pi 5

# Start Ollama service
ollama serve
```

### 4. Configure Environment

**Quick Security Setup (Recommended):**

```bash
# Run interactive security setup script
./setup-security.sh
```

This will:
- Generate a secure API key
- Create `.env` file from template
- Set up IMAP credentials
- Create secrets directory for Docker
- Set secure file permissions

**Manual Configuration:**

```bash
# Copy example configuration
cp .env.example .env

# Edit configuration
nano .env
```

**For detailed security configuration, see [SECURITY_GUIDE.md](SECURITY_GUIDE.md)**

**Essential Configuration:**

```env
# SECURITY: Generate an API key for authentication
# Generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'
API_KEY=your_secure_api_key_here

# IMAP Settings (example for Gmail)
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USERNAME=your_email@gmail.com
IMAP_PASSWORD=your_app_password

# AI Model
AI_MODEL=mistral:7b-instruct-q4_0

# SAFE MODE: Start with true, set to false after testing
SAFE_MODE=true
```

**For Gmail:** Create an [App Password](https://support.google.com/accounts/answer/185833)

### 5. Start the Application

```bash
# Activate virtual environment
source venv/bin/activate

# Start MailJaeger
python -m uvicorn src.main:app --host 127.0.0.1 --port 8000

# Or use the simpler command:
python -m src.main
```

The application will be available at **http://localhost:8000**

### 6. First Login

When you open the dashboard, you'll see a secure login screen:
- Enter the `API_KEY` value from your `.env` file
- The key is stored in sessionStorage (cleared when you close the tab/browser)
- For added security, the key is never saved permanently on disk

## Usage

### Web Dashboard

Access the dashboard at **http://localhost:8000**

The dashboard provides:
- 📊 Real-time statistics (total emails, action required, spam filtered)
- 📧 Email list with filtering and sorting
- 🔍 Detailed email view with AI analysis and tasks
- ⚡ Manual processing trigger
- 💚 System health monitoring

### API Access

For API access, include your API key in requests:

```bash
# Example: Trigger processing
curl -X POST http://localhost:8000/api/processing/trigger \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### API Documentation

Interactive API documentation:
- **Swagger UI**: http://localhost:8000/api/docs
- **ReDoc**: http://localhost:8000/api/redoc

### Approval Workflow

MailJaeger supports an optional **approval workflow** for IMAP mailbox changes, providing an extra layer of control before actions are executed.

#### Enabling Approval Mode

Set `REQUIRE_APPROVAL=true` in your `.env` file to enable the approval workflow:

```bash
REQUIRE_APPROVAL=true
```

When enabled:
- All proposed IMAP actions (move, delete, mark as read) are queued as **Pending Actions**
- Actions require manual approval before execution
- The system will **NOT** automatically modify your mailbox

#### Using the Dashboard

1. **Navigate to Pending Actions Tab**
   - Open the dashboard at http://localhost:8000
   - Click the "Pending Actions" tab in the navigation

2. **Review Pending Actions**
   - Each action shows: email ID, action type, target folder, reason, status
   - Filter by status (PENDING, APPROVED, REJECTED, APPLIED) or action type

3. **Approve or Reject Actions**
   - **Per-row buttons**: Approve ✓ or Reject ✗ individual actions
   - **Batch operations**:
     - "Approve all on page" - approves all pending actions on current page
     - "Reject all on page" - rejects all pending actions on current page

4. **Apply Approved Actions**
   - Click "Apply" button on individual approved actions
   - Or use "Apply approved (batch)" to execute up to 100 approved actions at once
   - Actions are executed only when explicitly applied

#### Viewing Email Actions

In the email detail view:
- Look for the "Proposed Mailbox Actions" section
- Shows all pending actions related to that specific email
- Displays action status and timestamps

#### API Examples

**List pending actions:**
```bash
curl -X GET "http://localhost:8000/api/pending-actions?status=PENDING" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Approve actions:**
```bash
curl -X POST http://localhost:8000/api/pending-actions/approve \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"action_ids": [1, 2, 3], "approved_by": "admin"}'
```

**Reject actions:**
```bash
curl -X POST http://localhost:8000/api/pending-actions/reject \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"action_ids": [4, 5], "approved_by": "admin"}'
```

**Apply approved actions:**
```bash
curl -X POST http://localhost:8000/api/pending-actions/apply \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"max_count": 50}'
```

#### Data Retention

Configure retention policies for old data:

```bash
# Days to retain processed emails (0 = keep forever)
RETENTION_DAYS_EMAILS=90

# Days to retain completed/rejected actions (0 = keep forever)
RETENTION_DAYS_ACTIONS=30

# Days to retain audit logs (0 = keep forever)
RETENTION_DAYS_AUDIT=180
```

**Automatic Purge:**
- Runs daily at 03:30 (local time)
- Deletes data older than configured retention periods
- PENDING and APPROVED actions are never auto-deleted

**Manual Purge (Admin):**
```bash
# Dry run (preview what would be deleted)
curl -X POST http://localhost:8000/api/purge \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'

# Execute purge
curl -X POST http://localhost:8000/api/purge \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false}'
```

## 🔒 Security Configuration

### Default Security Settings

MailJaeger ships with secure defaults optimized for local deployment:

| Setting | Default | Purpose |
|---------|---------|---------|
| `API_KEY` | Empty | **CRITICAL**: Set this for authentication |
| `SERVER_HOST` | `127.0.0.1` | Localhost-only (not publicly accessible) |
| `CORS_ORIGINS` | `localhost:8000,127.0.0.1:8000` | Restrictive CORS policy |
| `SAFE_MODE` | `true` | No destructive IMAP actions (dry-run) |
| `REQUIRE_APPROVAL` | `false` | Manual approval for IMAP changes |
| `STORE_EMAIL_BODY` | `false` | Data minimization (privacy) |
| `MARK_AS_READ` | `false` | Keeps emails unread |
| `DELETE_SPAM` | `false` | Moves to quarantine instead of deletion |
| `TRUST_PROXY` | `false` | Honors X-Forwarded-* headers only when enabled |
| `RETENTION_DAYS_*` | `0` | Keep all data forever (no auto-deletion) |

### Production Checklist

Before using MailJaeger in production:

- [ ] **Generate API Key**: `python -c 'import secrets; print(secrets.token_urlsafe(32))'`
- [ ] **Set API_KEY in .env**: Never leave empty
- [ ] **Test with SAFE_MODE=true**: Verify processing works
- [ ] **Disable SAFE_MODE**: Set to `false` after testing
- [ ] **Review Quarantine Folder**: Check `QUARANTINE_FOLDER` location
- [ ] **Privacy Settings**: Decide on `STORE_EMAIL_BODY` setting
- [ ] **Backup Strategy**: Plan for database backups

### External Access (Optional)

⚠️ **WARNING**: Only expose MailJaeger externally if you understand the security implications.

To expose externally:

1. **REQUIRED**: Set a strong `API_KEY` (32+ characters)
2. Set `SERVER_HOST=0.0.0.0` in `.env`
3. Update `CORS_ORIGINS` to include your domain
4. Use reverse proxy (nginx/Caddy) with HTTPS
5. Configure firewall rules
6. Consider VPN or Tailscale for secure access

**Example for Docker external access:**
```yaml
# docker-compose.yml
ports:
  - "8000:8000"  # Instead of "127.0.0.1:8000:8000"
environment:
  - API_KEY=your_very_secure_random_generated_key_here
  - SERVER_HOST=0.0.0.0
  - CORS_ORIGINS=https://mail.yourdomain.com
```

### Authentication

All API endpoints (except `/api/health`) require authentication:

```bash
# Include Bearer token in requests
curl -H "Authorization: Bearer YOUR_API_KEY" \
  http://localhost:8000/api/dashboard
```

The web dashboard will prompt for the API key on first access and store it securely in your browser's localStorage.

### Security Features

**Built-in Protection:**
- ✅ Token-based authentication on all endpoints
- ✅ Localhost binding by default (127.0.0.1)
- ✅ Restrictive CORS (no wildcard origins)
- ✅ Credential filtering in all logs
- ✅ Error message sanitization
- ✅ Strict input validation
- ✅ Safe mode for IMAP actions
- ✅ Complete audit trail

**Privacy Protection:**
- ✅ No cloud services (100% local)
- ✅ No telemetry or external tracking
- ✅ Data minimization (bodies not stored by default)
- ✅ Local AI processing only
- ✅ Secure credential handling

## How It Works

### Email Processing Workflow

1. **Retrieval**: Connects to IMAP and fetches unread emails
2. **AI Analysis**: Each email is analyzed for:
   - German summary
   - Category (Klinik, Forschung, Privat, Verwaltung, Unklar)
   - Spam probability
   - Action required status
   - Priority (LOW, MEDIUM, HIGH)
   - Extracted tasks with due dates
   - Suggested folder
3. **Spam Classification**: Combines AI analysis with heuristics
4. **Mailbox Actions** (respects SAFE_MODE setting):
   - **Safe Mode ON** (default): Analysis only, no destructive actions
   - **Safe Mode OFF**:
     - Spam → Moved to Quarantine folder (unless DELETE_SPAM=true)
     - Non-spam → Optionally marked read (if MARK_AS_READ=true), moved to Archive
     - Action required → Flagged
5. **Persistence**: Stored in local database with full audit trail
6. **Learning**: System learns from folder movements and improves suggestions

### Automated Scheduling

- **Default**: Runs daily at 08:00 (Europe/Berlin timezone)
- **Configurable**: Change `SCHEDULE_TIME` in `.env`
- **Manual Trigger**: Use API or run manually

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        MailJaeger                           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐            │
│  │   IMAP   │───▶│    AI    │───▶│ Database │            │
│  │ Service  │    │ Analysis │    │ (SQLite) │            │
│  └──────────┘    └──────────┘    └──────────┘            │
│                                                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐            │
│  │ Learning │    │  Search  │    │Scheduler │            │
│  │  System  │    │  Engine  │    │ Service  │            │
│  └──────────┘    └──────────┘    └──────────┘            │
│                                                             │
│  ┌─────────────────────────────────────────────┐          │
│  │           FastAPI REST API                  │          │
│  └─────────────────────────────────────────────┘          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                         ▲
                         │
                  ┌──────┴──────┐
                  │   Ollama    │
                  │  (Local LLM)│
                  └─────────────┘
```

## Performance Optimization

### For Raspberry Pi 5

1. **Use SSD instead of SD card** for database and search index
2. **Adjust batch size**: Set `MAX_EMAILS_PER_RUN` to 50-100 if processing is slow
3. **Reduce AI timeout**: Set `AI_TIMEOUT=60` if model responds faster
4. **Disable body storage**: Set `STORE_EMAIL_BODY=false` to save space
5. **Monitor resources**: Use `htop` to check CPU/memory usage

```bash
# Monitor system resources
htop

# Check Ollama logs
journalctl -u ollama -f
```

## Security & Privacy

- ✅ **No cloud services**: Everything runs locally
- ✅ **No telemetry**: Zero external communication except IMAP
- ✅ **Encrypted credentials**: Passwords never logged
- ✅ **Audit trail**: Complete record of all actions
- ✅ **Data sovereignty**: Your data stays on your device

## Troubleshooting

### IMAP Connection Issues

```bash
# Test IMAP connection
openssl s_client -connect imap.gmail.com:993

# Check credentials in .env
cat .env | grep IMAP
```

### AI Service Not Responding

```bash
# Check Ollama status
ollama list

# Restart Ollama
sudo systemctl restart ollama

# Check if model is loaded
curl http://localhost:11434/api/tags
```

### Database Issues

```bash
# Reset database (WARNING: deletes all data)
rm mailjaeger.db
python -m src.main
```

### Performance Issues

```bash
# Check system resources
free -h
df -h
ps aux | grep python

# Reduce concurrent processing
# Edit .env and set MAX_EMAILS_PER_RUN=50
```

## Development

### Running in Development Mode

```bash
# Enable debug logging
export DEBUG=true
export LOG_LEVEL=DEBUG

# Run with auto-reload
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

## 🚀 Production Deployment

### Security Checklist

Before deploying to production:

- [ ] **API Key Set**: Generate with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`
- [ ] **HTTPS Enabled**: Deploy behind reverse proxy with valid SSL
- [ ] **Firewall Configured**: Block direct access to ports 8000 and 11434
- [ ] **Safe Mode Tested**: Test with `SAFE_MODE=true` before enabling writes
- [ ] **Credentials Secured**: Use Docker secrets or secure env files
- [ ] **CORS Configured**: Set `CORS_ORIGINS` to your domain only
- [ ] **Rate Limiting Active**: Verify with repeated requests
- [ ] **Monitoring Setup**: Monitor logs and health endpoint

### Production Docker Deployment

```bash
# Create secrets
mkdir -p secrets && chmod 700 secrets
python -c 'import secrets; print(secrets.token_urlsafe(32))' > secrets/api_key.txt
chmod 600 secrets/api_key.txt

# Deploy
docker compose -f docker-compose.prod.yml up -d
```

### Reverse Proxy Setup

See [docs/reverse-proxy-examples.md](docs/reverse-proxy-examples.md) for:
- Nginx configuration with rate limiting  
- Caddy configuration with auto-SSL
- Traefik Docker configuration

**Important:** Set `TRUST_PROXY=true` when behind a reverse proxy!

### API Key Rotation

```bash
# Multiple keys (comma-separated)
API_KEY=old_key,new_key,another_key

# Or file-based
API_KEY_FILE=/run/secrets/mailjaeger_api_keys
```

Rotation process:
1. Add new key alongside old
2. Deploy/restart
3. Update clients to new key  
4. Remove old key
5. Deploy/restart again

### Monitoring

```bash
# Health check
curl http://localhost:8000/api/health

# Logs
docker logs mailjaeger-app -f

# Check auth failures
docker logs mailjaeger-app | grep "authentication"
```

### Backup

```bash
# Database
cp data/mailjaeger.db data/mailjaeger.db.$(date +%Y%m%d)

# Configuration (encrypt it!)
tar -czf config-$(date +%Y%m%d).tar.gz .env secrets/
gpg -c config-*.tar.gz
```

### Project Structure

```
MailJaeger/
├── src/
│   ├── api/              # API endpoints
│   ├── config.py         # Configuration management
│   ├── database/         # Database connection
│   ├── models/           # Data models
│   │   ├── database.py   # SQLAlchemy models
│   │   └── schemas.py    # Pydantic schemas
│   ├── services/         # Business logic
│   │   ├── ai_service.py
│   │   ├── email_processor.py
│   │   ├── imap_service.py
│   │   ├── learning_service.py
│   │   ├── scheduler.py
│   │   └── search_service.py
│   ├── utils/            # Utilities
│   └── main.py           # FastAPI application
├── requirements.txt      # Python dependencies
├── .env.example          # Example configuration
└── README.md             # This file
```

## Roadmap

Version 1.0 includes all core features as specified. Future enhancements:
- [ ] Web UI dashboard
- [ ] Mobile app support
- [ ] Calendar integration
- [ ] Attachment analysis
- [ ] Multi-language support beyond German
- [ ] Export/import functionality

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For issues and questions:
- **GitHub Issues**: https://github.com/pappydee/MailJaeger/issues
- **Documentation**: https://github.com/pappydee/MailJaeger/wiki

## Acknowledgments

Built with:
- [FastAPI](https://fastapi.tiangolo.com/) - Modern web framework
- [Ollama](https://ollama.ai/) - Local LLM serving
- [SQLAlchemy](https://www.sqlalchemy.org/) - Database ORM
- [Whoosh](https://whoosh.readthedocs.io/) - Full-text search
- [IMAPClient](https://imapclient.readthedocs.io/) - IMAP library