# Security Guide for MailJaeger

## Overview

MailJaeger is designed with security as a core principle. This guide documents all security features and best practices for production deployment.

## Security Architecture

### Defense in Depth

MailJaeger implements multiple layers of security:

1. **Authentication Layer**: Multi-key API authentication with constant-time comparison
2. **Network Layer**: Localhost-only binding, reverse proxy support, security headers
3. **Application Layer**: Input validation, rate limiting, safe mode
4. **Data Layer**: Credential redaction, data minimization, secure secrets management
5. **Monitoring Layer**: Audit logging, health checks, failed auth tracking

### Threat Model

**Assumptions:**
- Service is exposed on the internet (not LAN-only)
- Attacker may attempt: brute force, token theft, XSS/CSRF, SSRF, prompt injection, data exfiltration

**Mitigations:**
- All endpoints require authentication
- Rate limiting prevents brute force
- Session-only token storage prevents theft
- CSP and escaping prevent XSS
- Bearer tokens (not cookies) prevent CSRF
- AI output validation prevents prompt injection
- Log redaction prevents data leakage

## Authentication & Authorization

### API Key Management

**Generation:**
```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

**Configuration Methods:**

1. **Single Key (Environment Variable):**
```env
API_KEY=your_32_character_api_key_here
```

2. **Multiple Keys (Comma-Separated):**
```env
API_KEY=key1_active_now,key2_also_valid,key3_for_rotation
```

3. **File-Based Keys:**
```env
API_KEY_FILE=/run/secrets/mailjaeger_api_keys
```

File format (one key per line):
```
# Comments start with #
first_active_key_here
second_active_key_here
```

### Key Rotation

**Zero-Downtime Rotation Process:**

1. Add new key alongside old key:
   ```env
   API_KEY=old_key,new_key
   ```

2. Deploy/restart application - both keys now work

3. Update all clients to use new key

4. Remove old key:
   ```env
   API_KEY=new_key
   ```

5. Deploy/restart application

**Rotation Frequency:**
- Recommended: Every 90 days
- After suspected compromise: Immediately
- When employee leaves: Within 24 hours

### Protected Routes

All routes require authentication except:
- `/api/health` - Health check for monitoring
- Static assets (CSS, JS) - But frontend requires auth to load

Protected routes include:
- `/` - Frontend dashboard
- `/api/dashboard` - Dashboard data
- `/api/emails/*` - All email operations
- `/api/processing/*` - Processing controls
- `/api/settings` - Settings management

## Network Security

### Binding Configuration

**Development (local only):**
```env
SERVER_HOST=127.0.0.1  # Localhost only
SERVER_PORT=8000
```

**Production (behind reverse proxy):**
```env
SERVER_HOST=0.0.0.0    # Inside container
SERVER_PORT=8000
TRUST_PROXY=true       # Enable X-Forwarded-* headers
```

**Never expose directly to internet without reverse proxy!**

### Reverse Proxy Setup

MailJaeger must be deployed behind a reverse proxy for production. See `docs/reverse-proxy-examples.md` for:
- Nginx with rate limiting
- Caddy with auto-SSL
- Traefik with Docker

**Benefits:**
- SSL/TLS termination
- Additional rate limiting
- DDoS protection
- Load balancing (if multiple instances)

### Security Headers

Automatically added to all responses:

- **X-Content-Type-Options: nosniff** - Prevents MIME sniffing
- **X-Frame-Options: DENY** - Prevents clickjacking
- **Content-Security-Policy** - Prevents XSS and data injection
- **Referrer-Policy: strict-origin-when-cross-origin** - Controls referrer info
- **Permissions-Policy** - Restricts browser features
- **Strict-Transport-Security** - Forces HTTPS (when behind HTTPS proxy)

### CORS Configuration

**Restrictive by default:**
```env
# Only allow specific origins
CORS_ORIGINS=https://mail.yourdomain.com
```

**Multiple origins:**
```env
CORS_ORIGINS=https://mail.yourdomain.com,https://mail.otherdomain.com
```

**Never use wildcards in production!**

## Application Security

### Rate Limiting

**Built-in Rate Limits:**
- Root endpoint: Stricter limit on login page
- API Dashboard: 60 requests/minute
- Email Search: 30 requests/minute
- Email List: 60 requests/minute
- Processing Trigger: 5 requests/minute (very strict)
- Default: 200 requests/minute for other endpoints

**Configuration:**
Rate limits are per-client-IP and respect `X-Forwarded-For` when `TRUST_PROXY=true`.

**Testing:**
```bash
# Should eventually return 429
for i in {1..100}; do 
    curl -H "Authorization: Bearer $API_KEY" https://your-domain.com/api/dashboard
done
```

### Request Size Limits

**Default: 10MB maximum request body**

Prevents large payload attacks and DoS via memory exhaustion.

Configured in middleware (can be adjusted in code if needed).

### Input Validation

**AI Output Validation:**

1. **Category Allowlist**: Only predefined categories accepted
   - Valid: Klinik, Forschung, Privat, Verwaltung, Unklar
   - Invalid → Default: Unklar

2. **Priority Allowlist**: Only LOW, MEDIUM, HIGH
   - Invalid → Default: LOW

3. **Folder Allowlist**: AI can only suggest approved folders
   - Valid: Archive, Klinik, Forschung, Privat, Verwaltung, Important, Later
   - Invalid → Default: Archive
   - **Prevents prompt injection attacks** where AI suggests malicious folder operations

4. **Task Limits**: Maximum 10 tasks per email

5. **String Sanitization**: All strings truncated and sanitized
   - Removes control characters
   - Limits length
   - Prevents injection attacks

### Safe Mode

**Purpose**: Prevents destructive IMAP operations during testing

**Configuration:**
```env
SAFE_MODE=true  # Default - dry run only
```

**When Safe Mode is Enabled:**
- No emails marked as read
- No emails moved to folders
- No emails deleted
- All operations logged but not executed

**Production Recommendations:**
1. Start with `SAFE_MODE=true`
2. Verify AI analysis is accurate
3. Test for at least 1 week
4. Set `SAFE_MODE=false` only after verification

### Mail Action Controls

**Additional Safety Controls:**

```env
# Mark emails as read (only when SAFE_MODE=false)
MARK_AS_READ=false

# Delete spam (false = use quarantine folder instead)
DELETE_SPAM=false

# Quarantine folder for spam (safer than deletion)
QUARANTINE_FOLDER=Quarantine
```

**Folder Operations Hierarchy:**
1. System validates AI output
2. Safe mode check
3. Action-specific toggles (mark_as_read, delete_spam)
4. Only then execute IMAP command

## Secrets Management

### Environment Variables

**Minimum Required:**
```env
API_KEY=your_api_key
IMAP_HOST=imap.gmail.com
IMAP_USERNAME=you@gmail.com
IMAP_PASSWORD=your_password
```

### Docker Secrets

**Production Setup:**

1. Create secrets directory:
```bash
mkdir -p secrets
chmod 700 secrets
```

2. Store secrets:
```bash
echo "your_api_key" > secrets/api_key.txt
echo "your_imap_password" > secrets/imap_password.txt
chmod 600 secrets/*.txt
```

3. Use in Docker Compose:
```yaml
secrets:
  mailjaeger_api_key:
    file: ./secrets/api_key.txt
  imap_password:
    file: ./secrets/imap_password.txt

services:
  mailjaeger:
    secrets:
      - mailjaeger_api_key
      - imap_password
    environment:
      - API_KEY_FILE=/run/secrets/mailjaeger_api_key
      - IMAP_PASSWORD_FILE=/run/secrets/imap_password
```

### Credential Redaction

**Automatic Log Redaction:**

All logs are filtered to remove:
- Passwords (any variant: password, passwd, pwd)
- API keys and tokens
- Authorization headers
- Email addresses in credential context
- Long email bodies (truncated)

**Patterns Redacted:**
- `password: xxx` → `password: [REDACTED]`
- `Bearer token123` → `Bearer [REDACTED]`
- `Authorization: xxx` → `Authorization: [REDACTED]`
- `API_KEY=xxx` → `API_KEY=[REDACTED]`

**Defense in Depth:**
Even if code tries to log credentials, the logging filter will catch it.

## Data Protection

### Data Minimization

**Email Body Storage:**
```env
# Default: false (privacy-first)
STORE_EMAIL_BODY=false
```

Only metadata and AI analysis results are stored by default:
- Subject, sender, date
- AI summary and classification
- Tasks extracted
- No full email body

**When to Enable:**
Only if you need full-text search on email content.

### Attachment Storage

```env
# Default: false
STORE_ATTACHMENTS=false
```

**Considerations:**
- Disk space consumption
- Malware risk
- Privacy concerns

### Database Security

**Permissions:**
```bash
# SQLite database should not be world-readable
chmod 600 data/mailjaeger.db

# Data directory
chmod 700 data/
```

**Backup Encryption:**
```bash
# Backup
tar -czf backup.tar.gz data/

# Encrypt
gpg -c backup.tar.gz

# Delete unencrypted
shred -u backup.tar.gz
```

## Monitoring & Logging

### Audit Logging

All email processing actions are logged with:
- Timestamp
- Email message ID
- Actions taken
- Safe mode status
- AI classification results

**Example:**
```json
{
  "event_type": "EMAIL_PROCESSED",
  "email_message_id": "<msg@example.com>",
  "description": "Email processed: spam=false, action_required=true, safe_mode=true",
  "data": {
    "category": "Klinik",
    "priority": "HIGH",
    "actions": ["safe_mode_skip"],
    "safe_mode": true
  }
}
```

### Health Monitoring

**Health Endpoint:**
```bash
curl http://localhost:8000/api/health
```

**Response:**
```json
{
  "status": "healthy",
  "checks": {
    "mail_server": {"status": "healthy"},
    "ai_service": {"status": "healthy"},
    "database": {"status": "healthy"},
    "scheduler": {"status": "running"}
  }
}
```

**Monitoring Recommendations:**
- Check health endpoint every 30 seconds
- Alert on 3 consecutive failures
- Monitor authentication failures
- Track processing run success rate

### Failed Authentication Tracking

All failed auth attempts are logged with:
- Timestamp
- Requested path
- Source IP (when behind proxy with TRUST_PROXY=true)

**Example:**
```
2024-02-13 22:00:15 - src.middleware.auth - WARNING - Failed authentication attempt for /api/dashboard from 192.168.1.100
```

**Monitoring:**
```bash
# Check for brute force attempts
docker logs mailjaeger-app | grep "Failed authentication" | tail -20
```

## Incident Response

### Suspected Breach

1. **Immediately rotate API keys**:
   - Generate new keys
   - Deploy with new keys only
   - Invalidate all old keys

2. **Check audit logs**:
   ```bash
   docker logs mailjaeger-app > incident-$(date +%Y%m%d).log
   grep -i "authentication\|error" incident-*.log
   ```

3. **Review database** for unauthorized changes:
   ```bash
   sqlite3 data/mailjaeger.db "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT 100"
   ```

4. **Check IMAP account** for unexpected actions

5. **Update all credentials** (IMAP, API keys)

### Brute Force Detection

**Signs:**
- Multiple failed auth attempts from same IP
- Rapid succession of auth failures
- Different API keys attempted

**Response:**
1. Check logs for source IP
2. Block IP at firewall/proxy level
3. Consider reducing rate limits
4. Rotate API keys if any may be compromised

## Security Checklist

Use this before production deployment:

- [ ] API keys generated and securely stored
- [ ] HTTPS configured via reverse proxy
- [ ] Firewall blocks direct access to ports 8000, 11434
- [ ] `TRUST_PROXY=true` when behind reverse proxy
- [ ] `CORS_ORIGINS` set to specific domain(s)
- [ ] `DEBUG=false` in production
- [ ] `SAFE_MODE=true` for initial testing period
- [ ] Docker secrets used for sensitive config
- [ ] Database file permissions restrictive (600)
- [ ] Backup strategy in place with encryption
- [ ] Monitoring configured (health checks, log aggregation)
- [ ] Tested authentication (valid and invalid keys)
- [ ] Tested rate limiting (429 errors return correctly)
- [ ] Reviewed audit logs after test period
- [ ] Security headers verified (use securityheaders.com)
- [ ] SSL/TLS verified (use ssllabs.com/ssltest/)
- [ ] Incident response plan documented
- [ ] Key rotation schedule established

## Security Updates

**Stay Current:**
- Monitor GitHub security advisories
- Update dependencies regularly
- Review CHANGELOG for security fixes
- Subscribe to security mailing lists for:
  - FastAPI
  - Ollama
  - SQLAlchemy
  - Python

**Update Process:**
1. Review changelogs
2. Test in staging environment
3. Backup production database
4. Deploy during low-traffic period
5. Monitor logs after deployment

## Contact

For security issues:
- **Private disclosure**: Create a GitHub security advisory
- **Questions**: Open a GitHub issue (for non-sensitive matters)

Do not publicly disclose security vulnerabilities until they are fixed.
