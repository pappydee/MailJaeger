# Approval Workflow

## Overview

MailJaeger supports an optional approval workflow that allows you to review and approve IMAP mailbox actions before they are applied. This provides an additional safety layer beyond safe mode.

## Configuration

### Environment Variables

- `SAFE_MODE` (default: `true`): When enabled, prevents all IMAP mailbox changes (dry-run mode)
- `REQUIRE_APPROVAL` (default: `false`): When enabled, enqueues pending actions instead of executing them immediately

### Behavior Matrix

| SAFE_MODE | REQUIRE_APPROVAL | Behavior |
|-----------|------------------|----------|
| `true`    | any             | No IMAP actions executed, no pending actions created |
| `false`   | `true`          | Pending actions enqueued, require approval before execution |
| `false`   | `false`         | IMAP actions executed immediately (normal mode) |

**Note**: `SAFE_MODE=true` always takes precedence over `REQUIRE_APPROVAL`.

## Workflow

### 1. Email Processing with Approval Required

When `REQUIRE_APPROVAL=true` and `SAFE_MODE=false`:

1. Emails are processed and analyzed by AI
2. Instead of executing IMAP actions immediately, `PendingAction` records are created
3. Actions are marked with status `PENDING`
4. Email records show `queued_pending_actions` in their `actions_taken` field

### 2. Review Pending Actions

List all pending actions:
```bash
GET /api/pending-actions?status=PENDING
```

Get a single pending action:
```bash
GET /api/pending-actions/{action_id}
```

### 3. Approve or Reject Actions

Approve a single action:
```bash
POST /api/pending-actions/{action_id}/approve
{
  "approve": true
}
```

Reject a single action:
```bash
POST /api/pending-actions/{action_id}/approve
{
  "approve": false
}
```

### 4. Preview Approved Actions (Dry Run)

Preview what would be applied:
```bash
GET /api/pending-actions/preview
```

Or use dry-run mode when applying:
```bash
POST /api/pending-actions/apply
{
  "dry_run": true
}
```

### 5. Apply Approved Actions

Apply all approved actions:
```bash
POST /api/pending-actions/apply
{
  "dry_run": false
}
```

Apply a single approved action:
```bash
POST /api/pending-actions/{action_id}/apply
{
  "dry_run": false
}
```

## Action Types

### For Spam Emails

When an email is classified as spam with `REQUIRE_APPROVAL=true`:
- **Action**: `MOVE_FOLDER`
- **Target**: Always moves to `QUARANTINE_FOLDER` (never deletes, even if `DELETE_SPAM=true`)

### For Non-Spam Emails

When an email is not spam with `REQUIRE_APPROVAL=true`:

1. **MARK_READ** (if `MARK_AS_READ=true`)
   - Marks the email as read
   
2. **MOVE_FOLDER** (always)
   - Target: `ARCHIVE_FOLDER`
   
3. **ADD_FLAG** (if AI determines `action_required=true`)
   - Adds a flag to the email for follow-up

## Pending Action Status Flow

```
PENDING → APPROVED → APPLIED
   ↓
REJECTED
   ↓
FAILED (on apply error)
```

- `PENDING`: Action created, awaiting approval
- `APPROVED`: Action approved by user, ready to apply
- `REJECTED`: Action rejected by user, will not be applied
- `APPLIED`: Action successfully executed on IMAP mailbox
- `FAILED`: Action execution failed (see `error_message` field)

## Security Considerations

1. **Authentication Required**: All approval workflow endpoints require authentication via Bearer token
2. **Fail-Closed**: If authentication is not configured, all requests are denied
3. **Audit Trail**: All actions are logged in the audit log
4. **No Credential Exposure**: API responses never include IMAP credentials
5. **Safe Defaults**: Both `SAFE_MODE` and `REQUIRE_APPROVAL` default to safe values

## Example Workflow

### Enable Approval Workflow

1. Set environment variables:
```bash
SAFE_MODE=false
REQUIRE_APPROVAL=true
```

2. Process emails (they will be analyzed but actions will be queued):
```bash
POST /api/processing/trigger
```

3. Review pending actions:
```bash
GET /api/pending-actions?status=PENDING
```

4. Approve selected actions:
```bash
POST /api/pending-actions/123/approve
{
  "approve": true
}
```

5. Preview what will be applied:
```bash
GET /api/pending-actions/preview
```

6. Apply all approved actions:
```bash
POST /api/pending-actions/apply
```

## Migration from Safe Mode

If you're currently using `SAFE_MODE=true` for testing:

1. Keep `SAFE_MODE=true` during initial setup and testing
2. Once confident, set `SAFE_MODE=false` and `REQUIRE_APPROVAL=true`
3. Process emails and review pending actions
4. When comfortable, set `REQUIRE_APPROVAL=false` for automatic processing

## Troubleshooting

### Actions not being created

- Check that `SAFE_MODE=false`
- Check that `REQUIRE_APPROVAL=true`
- Verify emails are being processed (check processing runs)

### Actions remain PENDING

- Ensure actions are approved before attempting to apply
- Check that IMAP credentials are valid
- Review error messages in `error_message` field if status is `FAILED`

### Actions fail to apply

- Verify IMAP server is accessible
- Check that target folders exist
- Ensure UID is still valid (email hasn't been deleted)
