# Security Hardening Verification Checklist

Run through this checklist to verify all security features are working correctly.

## Pre-Deployment Verification

### 1. Configuration ✓

```bash
# Verify .env file exists and has secure permissions
[ -f .env ] && [ "$(stat -c %a .env)" = "600" ] && echo "✓ .env secure" || echo "✗ .env issues"

# Verify API key is set
grep -q "^API_KEY=..*" .env && echo "✓ API key configured" || echo "✗ API key missing"

# Verify secrets directory
[ -d secrets ] && [ "$(stat -c %a secrets)" = "700" ] && echo "✓ secrets dir secure" || echo "✗ secrets issues"
```

### 2. Authentication ✓

```bash
# Start the application
python -m uvicorn src.main:app --host 127.0.0.1 --port 8000 &
APP_PID=$!
sleep 5

# Test unauthenticated access (should fail with 401)
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ | grep -q "401" && echo "✓ Root requires auth" || echo "✗ Auth not enforced"

curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/dashboard | grep -q "401" && echo "✓ Dashboard requires auth" || echo "✗ Dashboard not protected"

# Test with invalid key (should fail)
curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer wrong_key" http://localhost:8000/api/dashboard | grep -q "401" && echo "✓ Invalid key rejected" || echo "✗ Invalid key accepted"

# Cleanup
kill $APP_PID 2>/dev/null
```

### 3. Security Headers ✓

```bash
# Check security headers
curl -sI http://localhost:8000/api/health | grep -E "X-Content-Type-Options|X-Frame-Options|Content-Security-Policy" && echo "✓ Security headers present" || echo "✗ Headers missing"
```

### 4. Log Redaction ✓

```bash
# Test credential redaction in code
python3 << 'PYTHON'
from src.utils.logging import SensitiveDataFilter

filter = SensitiveDataFilter()

# Test password redaction
msg = "password: secret123"
filtered = filter._redact_message(msg)
assert "[REDACTED]" in filtered and "secret123" not in filtered

# Test API key redaction
msg = "API_KEY=supersecret"
filtered = filter._redact_message(msg)
assert "[REDACTED]" in filtered and "supersecret" not in filtered

# Test Bearer token redaction
msg = "Authorization: Bearer token123"
filtered = filter._redact_message(msg)
assert "[REDACTED]" in filtered and "token123" not in filtered

print("✓ Log redaction working")
PYTHON
```

### 5. AI Output Validation ✓

```bash
# Test folder allowlist
python3 << 'PYTHON'
from src.services.ai_service import AIService
import os

os.environ["AI_ENDPOINT"] = "http://localhost:11434"
service = AIService()

# Valid folder should pass
assert service._validate_folder("Archive") == "Archive"

# Invalid folder should default to Archive
assert service._validate_folder("../etc/passwd") == "Archive"
assert service._validate_folder("DELETE_ALL") == "Archive"

print("✓ Folder allowlist working")
PYTHON
```

### 6. Rate Limiting ✓

```bash
# This test requires the app to be running
# Should eventually return 429 after hitting rate limit
echo "Testing rate limiting (this may take a moment)..."
for i in {1..50}; do
    RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer test_key" http://localhost:8000/api/dashboard 2>/dev/null)
    if [ "$RESPONSE" = "429" ]; then
        echo "✓ Rate limiting working (got 429)"
        break
    fi
done
```

### 7. Docker Secrets Support ✓

```bash
# Verify file-based config methods exist
grep -q "API_KEY_FILE" src/config.py && echo "✓ API_KEY_FILE supported" || echo "✗ Missing"
grep -q "IMAP_PASSWORD_FILE" src/config.py && echo "✓ IMAP_PASSWORD_FILE supported" || echo "✗ Missing"
grep -q "get_api_keys" src/config.py && echo "✓ Multi-key support" || echo "✗ Missing"
```

### 8. Safe Mode ✓

```bash
# Verify safe mode is default
grep -q 'safe_mode.*default=True' src/config.py && echo "✓ Safe mode enabled by default" || echo "✗ Not default"
```

### 9. Documentation ✓

```bash
# Verify all documentation exists
[ -f SECURITY_GUIDE.md ] && echo "✓ Security guide" || echo "✗ Missing"
[ -f SECURITY_IMPLEMENTATION.md ] && echo "✓ Implementation doc" || echo "✗ Missing"
[ -f setup-security.sh ] && [ -x setup-security.sh ] && echo "✓ Setup script" || echo "✗ Missing"
[ -f docker-compose.prod.yml ] && echo "✓ Production compose" || echo "✗ Missing"
[ -f docs/reverse-proxy-examples.md ] && echo "✓ Proxy examples" || echo "✗ Missing"
```

### 10. Test Suite ✓

```bash
# Run security tests (if pytest is installed)
if command -v pytest &> /dev/null; then
    pytest tests/test_security.py -v --tb=short && echo "✓ Security tests pass" || echo "✗ Tests fail"
else
    echo "⚠ pytest not installed, skipping test suite"
fi
```

## Post-Deployment Verification

### Production Environment Checks

```bash
# 1. HTTPS is working
curl -I https://yourdomain.com 2>&1 | grep "200 OK" && echo "✓ HTTPS working" || echo "✗ HTTPS issue"

# 2. HTTP redirects to HTTPS
curl -I http://yourdomain.com 2>&1 | grep -E "301|302" && echo "✓ HTTP redirect" || echo "✗ No redirect"

# 3. Security headers present
curl -sI https://yourdomain.com | grep "Strict-Transport-Security" && echo "✓ HSTS enabled" || echo "✗ No HSTS"

# 4. Authentication required
curl -s -o /dev/null -w "%{http_code}" https://yourdomain.com/api/dashboard | grep "401" && echo "✓ Auth enforced" || echo "✗ No auth"

# 5. Ollama NOT accessible
curl -s -o /dev/null -w "%{http_code}" -m 5 https://yourdomain.com:11434 2>&1 | grep -qE "000|timeout" && echo "✓ Ollama not exposed" || echo "✗ Ollama accessible!"

# 6. Rate limiting works
for i in {1..100}; do
    RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $API_KEY" https://yourdomain.com/api/dashboard)
    if [ "$RESPONSE" = "429" ]; then
        echo "✓ Rate limiting active"
        break
    fi
done

# 7. Logs don't contain credentials
docker logs mailjaeger-app | grep -i "password\|api_key" | grep -q "\[REDACTED\]" && echo "✓ Credentials redacted" || echo "✗ Credentials in logs!"
```

## Security Scorecard

Test your deployment:

1. **SSL Labs Test**: https://www.ssllabs.com/ssltest/
   - Should get A or A+ rating

2. **Security Headers Test**: https://securityheaders.com/
   - Should get A rating

3. **Observatory by Mozilla**: https://observatory.mozilla.org/
   - Should get B+ or higher

## Manual Testing

### Test Authentication Flow

1. Open browser to https://yourdomain.com
2. Should see login screen (not dashboard)
3. Enter API key
4. Should load dashboard
5. Close tab and reopen
6. Should need to login again (sessionStorage)

### Test Rate Limiting

```bash
# Make 100 requests rapidly
for i in {1..100}; do
    curl -H "Authorization: Bearer $API_KEY" https://yourdomain.com/api/dashboard &
done
wait

# Should see some 429 responses in logs
docker logs mailjaeger-app | grep "429"
```

### Test Log Redaction

```bash
# Check logs don't contain real credentials
docker logs mailjaeger-app 2>&1 | grep -E "password|api.?key|token" | grep -v "\[REDACTED\]"
# Should return empty (no unredacted credentials)
```

### Test Safe Mode

1. Set `SAFE_MODE=true`
2. Trigger processing
3. Check logs - should see "SAFE MODE" messages
4. Verify no emails actually moved/marked

## All Checks Complete

Once all checks pass:

- [ ] All automated tests pass
- [ ] Authentication enforced on all routes  
- [ ] Security headers present
- [ ] Rate limiting active
- [ ] Credentials redacted in logs
- [ ] HTTPS working with valid certificate
- [ ] Ollama not publicly accessible
- [ ] Safe mode tested
- [ ] Documentation reviewed
- [ ] Incident response plan ready

**Status: Ready for Production Deployment ✅**
