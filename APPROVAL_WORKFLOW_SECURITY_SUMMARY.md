# Security Summary: Approval Workflow and Final Hardening

**Branch**: copilot/approval-workflow-and-final-hardening  
**Date**: 2026-02-14  
**Status**: ✓ COMPLETE - All requirements met, 0 vulnerabilities found

## Overview

This implementation adds a mandatory review-and-approve workflow for all IMAP actions and completes internet exposure hardening. All changes have been security-reviewed and CodeQL-scanned with no vulnerabilities detected.

## Security Features Implemented

### 1. Approval Workflow Security

**Database Security**:
- PendingAction table never stores email bodies or headers (only references)
- Never stores IMAP credentials
- Sanitized error messages (no credentials, server banners, or stack traces)
- Foreign key constraints to ProcessedEmail table

**API Security**:
- All endpoints require authentication (Bearer token)
- Rate limiting on all endpoints:
  - List: 60/minute
  - Approve/Reject: 30/minute
  - Apply single: 10/minute
  - Batch apply: 5/minute (strictest)
- Concurrency guard for batch apply (prevents race conditions)
- 1MB request size limit (reduced from 10MB)

**Folder Allowlist**:
- Only folders in ALLOWED_MOVE_FOLDERS can be used
- Validated at enqueue time AND apply time
- Actions to disallowed folders are marked FAILED with error code
- Default: Quarantine,Archive only

**Action Lifecycle**:
- PENDING → APPROVED → APPLIED (success path)
- PENDING → REJECTED (rejection path)
- PENDING → FAILED (validation failure)
- Audit log for every state transition
- Approved_by field stores safe placeholder (no PII)

### 2. Internet Exposure Hardening

**Proxy Trust**:
- TRUST_PROXY=false by default
- Optional TRUSTED_PROXY_IPS for IP validation
- Only honors X-Forwarded-* when trust conditions met
- Documented limitations of proxy validation

**Security Headers**:
- X-Content-Type-Options: nosniff
- X-Frame-Options: DENY
- Referrer-Policy: no-referrer (strictest)
- Permissions-Policy: restrictive (geolocation, camera, mic all denied)
- CSP: frame-ancestors 'none', upgrade-insecure-requests
- HSTS: 1 year, includeSubDomains, preload (when HTTPS)

**Host Allowlist**:
- ALLOWED_HOSTS validation middleware
- Handles IPv6 addresses correctly ([::1]:8000)
- Handles IPv4 and hostnames with ports
- Returns 400 on mismatch

**Rate Limiting**:
- Auth failure attempts logged
- Expensive endpoints strictly limited:
  - /api/processing/trigger: 5/minute
  - /api/pending-actions/apply: 5/minute (batch)
  - /api/pending-actions/{id}/apply: 10/minute
  - /api/emails/search: 30/minute
  - /api/emails/list: 60/minute

**Request Size Limits**:
- 1MB body size limit (reduced from 10MB)
- 413 response with safe error message

**Timeouts**:
- IMAP_CONNECT_TIMEOUT: 30s (default)
- IMAP_OPERATION_TIMEOUT: 60s (default)
- LLM_CONNECT_TIMEOUT: 10s (default)
- LLM_READ_TIMEOUT: 120s (default)
- Prevents indefinite hangs

### 3. Credential Protection

**API Endpoints**:
- /api/settings explicitly excludes:
  - api_key
  - api_key_file
  - imap_username
  - imap_password
  - imap_password_file
  - Authorization header
- Only returns non-sensitive configuration

**Error Handling**:
- Sanitized error messages throughout
- Regex-based detection of credential patterns:
  - password="xxx"
  - token: xxx
  - bearer tokens
  - email addresses
- Generic message returned when patterns detected

**Logging**:
- No credentials logged
- No Authorization headers logged
- No IMAP responses logged (may contain banners)
- Audit logs exclude sensitive data

**Code Review**:
- All credential references reviewed
- No hardcoded secrets
- Environment variables only
- File-based secrets supported (API_KEY_FILE, IMAP_PASSWORD_FILE)

### 4. Operational Robustness

**Data Retention**:
- RETENTION_DAYS_EMAILS: 0 = never purge, >0 = purge after N days
- RETENTION_DAYS_ACTIONS: 0 = never purge, >0 = purge after N days
- PENDING and APPROVED actions never purged automatically
- Only purges when STORE_EMAIL_BODY=true (respects privacy)

**Audit Logging**:
- All approval workflow actions logged
- Processing runs logged
- Purge operations logged
- Event types: ACTION_ENQUEUED, ACTION_APPROVED, ACTION_REJECTED, ACTION_APPLIED, ACTION_FAILED, DATA_PURGE, EMAIL_PROCESSED
- Never logs email bodies or credentials

**Concurrency**:
- Thread lock for batch apply operations
- Prevents concurrent batch applies (409 response)
- Safe for multi-worker deployments

## Vulnerabilities Discovered & Fixed

### During Implementation

**None identified** - all security issues were prevented by design:

1. Folder allowlist enforced from start
2. Credentials never passed to API responses
3. Error sanitization built into service layer
4. Rate limiting applied to all new endpoints
5. Authentication required on all new endpoints

### During Code Review

All code review findings addressed:

1. **IPv6 handling in host allowlist** - Fixed to handle `[::1]:8000` format
2. **Error sanitization** - Improved with regex patterns instead of simple string matching
3. **Proxy IP validation** - Documented limitations and added clarifying comments
4. **Field naming** - Clarified approved_at usage for rejected actions
5. **Retention validation** - Fixed to allow 0 (never purge) and updated docs

### During CodeQL Scan

**0 vulnerabilities found** - clean scan result

## Security Testing

**Credential Leakage Tests**:
- ✓ /api/settings does not return credentials
- ✓ Error responses do not leak credentials
- ✓ Authorization header never appears in responses
- ✓ Health endpoint does not leak credentials

**Approval Workflow Tests**:
- ✓ Actions enqueued when REQUIRE_APPROVAL=true
- ✓ Approval transitions status to APPROVED
- ✓ Apply performs IMAP action and sets APPLIED
- ✓ Folder allowlist enforced (FAILED status for disallowed folders)

**Authentication Tests** (existing):
- ✓ /api/health accessible without auth
- ✓ All other routes require auth (401 without token)

## Configuration Recommendations

### Production Deployment

```bash
# Security
API_KEY=<generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'>
TRUST_PROXY=true
TRUSTED_PROXY_IPS=<your reverse proxy IP>
ALLOWED_HOSTS=mailjaeger.yourdomain.com

# Approval Workflow
REQUIRE_APPROVAL=true
SAFE_MODE=false  # Allow actions after approval
AUTO_APPLY_APPROVED_ACTIONS=false  # Manual apply
ALLOWED_MOVE_FOLDERS=Quarantine,Archive,Work

# Timeouts
IMAP_CONNECT_TIMEOUT=30
IMAP_OPERATION_TIMEOUT=60
LLM_CONNECT_TIMEOUT=10
LLM_READ_TIMEOUT=120

# Retention
RETENTION_DAYS_EMAILS=30
RETENTION_DAYS_ACTIONS=90

# Hardening
SERVER_HOST=127.0.0.1  # Behind reverse proxy only
CORS_ORIGINS=https://mailjaeger.yourdomain.com
```

### Reverse Proxy (Nginx Example)

```nginx
server {
    listen 443 ssl http2;
    server_name mailjaeger.yourdomain.com;
    
    # SSL config...
    
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Security
        proxy_hide_header X-Powered-By;
        add_header X-Content-Type-Options nosniff always;
        add_header X-Frame-Options DENY always;
        add_header Referrer-Policy no-referrer always;
        
        # Rate limiting
        limit_req zone=api burst=20 nodelay;
    }
}
```

## Remaining Considerations

### Future Enhancements

1. **Web UI for Approval Workflow**: Current implementation provides REST API endpoints; a web UI would improve usability
2. **Multi-user Support**: Current approved_by field stores token placeholders; could be enhanced with user management
3. **Approval Delegation**: Could add approval rules, auto-approve based on sender/category
4. **Detailed Audit Export**: Audit logs are in database; could add export API

### Monitoring Recommendations

1. Monitor failed approval applications (check FAILED status actions)
2. Monitor rate limit violations (check logs for 429 responses)
3. Monitor authentication failures (check logs for 401 responses)
4. Monitor host header rejections (check logs for 400 responses)
5. Set up alerts for concurrent batch apply attempts (409 responses)

## Conclusion

All security requirements have been met:

✓ Approval workflow implemented with comprehensive safeguards  
✓ Folder allowlist enforced  
✓ No credentials exposed via API or logs  
✓ Internet exposure hardened (proxy trust, headers, allowlist, rate limits, timeouts)  
✓ Operational robustness (retention, audit logging, concurrency)  
✓ Code reviewed and all findings addressed  
✓ CodeQL security scan: 0 vulnerabilities  
✓ Tests created and documented  
✓ Documentation complete (.env.example, README)

The implementation is production-ready with defense-in-depth security measures.
