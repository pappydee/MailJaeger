# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.1.x   | ✅ Current |
| 1.0.x   | ⚠️ Security fixes only |
| < 1.0   | ❌ No longer supported |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report security issues via [GitHub Security Advisories](https://github.com/pappydee/MailJaeger/security/advisories/new). This keeps the report private until a fix is ready.

Include in your report:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- Affected version(s)
- Any suggested mitigation (optional)

## Response Process

1. **Acknowledgement**: Within 48 hours of receipt
2. **Assessment**: Severity and impact evaluation within 5 business days
3. **Fix**: Patch developed and tested
4. **Release**: Patched version published with release notes
5. **Disclosure**: Coordinated public disclosure after fix is available

## Security Architecture

MailJaeger is designed to be private and secure by default:

- 100% local operation — no cloud services or telemetry
- API key authentication with constant-time comparison
- Browser login via HttpOnly, SameSite=Lax session cookie (key never stored in browser)
- Localhost-only binding by default (`127.0.0.1`)
- Safe mode prevents destructive IMAP actions until explicitly disabled
- All logs redacted to prevent credential leakage

For full operational security guidance see [SECURITY_GUIDE.md](SECURITY_GUIDE.md).

## Contact

- **Security advisories**: https://github.com/pappydee/MailJaeger/security/advisories
- **General questions**: Open a GitHub Discussion

