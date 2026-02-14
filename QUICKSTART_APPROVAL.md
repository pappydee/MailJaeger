# Quick Start: Approval Workflow

## What is the Approval Workflow?

The approval workflow lets you review email actions before they're executed on your IMAP mailbox. Instead of automatically moving, flagging, or marking emails, MailJaeger will queue these actions for your review.

## Quick Setup

### Step 1: Enable the Workflow

Add to your `.env` file:
```bash
SAFE_MODE=false
REQUIRE_APPROVAL=true
```

### Step 2: Process Emails

Trigger email processing:
```bash
curl -X POST http://localhost:8000/api/processing/trigger \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### Step 3: Review Pending Actions

List all pending actions:
```bash
curl http://localhost:8000/api/pending-actions?status=PENDING \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Response example:
```json
[
  {
    "id": 1,
    "email_id": 123,
    "action_type": "MOVE_FOLDER",
    "target_folder": "Archive",
    "status": "PENDING",
    "created_at": "2026-02-14T12:00:00",
    "email": {
      "subject": "Important Email",
      "sender": "sender@example.com",
      "category": "Klinik"
    }
  }
]
```

### Step 4: Approve Actions

Approve a single action:
```bash
curl -X POST http://localhost:8000/api/pending-actions/1/approve \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"approve": true}'
```

Or reject:
```bash
curl -X POST http://localhost:8000/api/pending-actions/1/approve \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"approve": false}'
```

### Step 5: Preview (Optional)

See what will be executed:
```bash
curl http://localhost:8000/api/pending-actions/preview \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### Step 6: Apply Approved Actions

Apply all approved actions:
```bash
curl -X POST http://localhost:8000/api/pending-actions/apply \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false}'
```

Or apply just one:
```bash
curl -X POST http://localhost:8000/api/pending-actions/1/apply \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false}'
```

## Configuration Options

| Setting | Default | Description |
|---------|---------|-------------|
| `SAFE_MODE` | `true` | Prevents all IMAP actions (overrides everything) |
| `REQUIRE_APPROVAL` | `false` | Queue actions for approval instead of executing |

## Behavior Matrix

| SAFE_MODE | REQUIRE_APPROVAL | What Happens |
|-----------|------------------|--------------|
| `true` | any | No actions, nothing queued (safest) |
| `false` | `true` | Actions queued for approval ✓ |
| `false` | `false` | Actions executed immediately |

## Common Workflows

### Safe Testing
```bash
SAFE_MODE=true          # Test without any changes
REQUIRE_APPROVAL=false  # Not needed when safe mode is on
```

### Approval Workflow (Recommended)
```bash
SAFE_MODE=false         # Allow actions
REQUIRE_APPROVAL=true   # But require approval first
```

### Automatic Processing
```bash
SAFE_MODE=false         # Allow actions
REQUIRE_APPROVAL=false  # Execute immediately
```

## Action Types Explained

### MOVE_FOLDER
Moves email to a folder (Archive, Quarantine, Spam)

### MARK_READ
Marks the email as read (if `MARK_AS_READ=true`)

### ADD_FLAG
Adds a flag/star to the email (if AI detects action needed)

## Tips

1. **Start with SAFE_MODE=true** to test without changes
2. **Use dry_run mode** before applying to verify
3. **Review spam actions carefully** - they go to Quarantine, not deleted
4. **Approve in batches** - process multiple emails, then approve all at once
5. **Check preview** before applying to see exactly what will happen

## Troubleshooting

### No pending actions created?
- Check `SAFE_MODE=false`
- Check `REQUIRE_APPROVAL=true`
- Verify emails were processed (check `/api/processing/runs`)

### Can't apply actions?
- Make sure they're APPROVED first
- Check IMAP credentials are valid
- Verify target folders exist

### Actions fail with "UID not found"?
- Email may have been deleted
- IMAP connection issue
- Check error_message field for details

## Need More Help?

See full documentation: `docs/approval-workflow.md`
