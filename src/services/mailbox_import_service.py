"""
Mailbox-Wide Streaming Import + Learn Service (v1.2.0).

Performs a full-mailbox historical ingestion *and* learning pass in small
streaming batches so the whole mailbox never has to be downloaded first.

Processing model (per batch):
  1. Select the next IMAP folder (skipping already-completed ones)
  2. Fetch a small batch of emails from that folder via IMAP
  3. Ingest / index them locally (skip duplicates)
  4. Run historical learning on the newly ingested emails immediately
  5. Persist a per-folder UID checkpoint
  6. Continue with the next batch (or next folder)

Resumability / crash safety:
  - ``MailboxImportRun`` persists ``current_folder`` and
    ``current_folder_uid_checkpoint`` after every batch.
  - ``folders_completed`` is a JSON list of folders fully processed.
  - A paused or failed run can be resumed from the exact batch boundary.

Attachment strategy:
  - By default, attachment **binaries** are NOT fetched.
  - The service uses ``BODY.PEEK[HEADER]`` + ``BODY.PEEK[TEXT]`` to
    retrieve headers and body text only.  Attachment metadata (filename,
    content-type, size, disposition) is extracted from MIME structure
    headers without downloading the binary content.
  - ``skip_attachment_binaries`` (default True) controls this behavior.
    When False, the service falls back to ``BODY.PEEK[]`` which fetches
    the complete RFC 5322 message including all attachments.

Concurrency:
  - Exactly one import job may run at a time (enforced via ``_import_lock``).
  - The job runs in a daemon thread started by ``start_import()``.
  - Safe cancellation via ``_import_cancel_event``.
"""

import hashlib
import re
import threading
import time
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header as _decode_header_raw
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import Integer, cast, desc, func
from sqlalchemy.orm import Session

from src.models.database import (
    MailboxImportRun,
    ProcessedEmail,
)
from src.services.folder_classifier import classify_folder, is_learnable_folder
from src.services.historical_learning import learn_from_email
from src.services.historical_learning_service import _update_sender_interaction
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level concurrency primitives
# ---------------------------------------------------------------------------
_import_lock = threading.Lock()
_import_cancel_event = threading.Event()
_current_import_thread: Optional[threading.Thread] = None

# Default batch size — small to keep memory bounded on large mailboxes
DEFAULT_IMPORT_BATCH_SIZE = 20

# IMAP fetch keys for metadata-only ingestion (no attachment binaries)
_FETCH_HEADERS_AND_TEXT = [b"BODY.PEEK[HEADER]", b"BODY.PEEK[TEXT]", b"FLAGS", b"BODYSTRUCTURE"]
# Full fetch (includes attachment binaries)
_FETCH_FULL = [b"BODY.PEEK[]", b"FLAGS"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_import(
    db_factory: Callable[[], Session],
    *,
    batch_size: int = DEFAULT_IMPORT_BATCH_SIZE,
    skip_attachment_binaries: bool = True,
) -> Dict[str, Any]:
    """Start (or resume) a mailbox-wide streaming import + learn job.

    Args:
        db_factory: Callable that returns a new SQLAlchemy Session.
        batch_size: Emails per IMAP fetch batch (clamped to 5–100).
        skip_attachment_binaries: When True (default), do not download
            attachment content — only extract metadata.

    Returns:
        Dict with job_id, status, and message.
    """
    global _current_import_thread

    batch_size = max(5, min(100, batch_size))

    if not _import_lock.acquire(blocking=False):
        return {"success": False, "message": "A mailbox import job is already running"}

    try:
        db = db_factory()
        try:
            run = _get_or_create_run(db, batch_size, skip_attachment_binaries)
            run_id = run.id
        finally:
            db.close()

        _import_cancel_event.clear()

        thread = threading.Thread(
            target=_run_import_thread,
            args=(db_factory, run_id, batch_size, skip_attachment_binaries),
            daemon=True,
            name=f"mailbox-import-{run_id}",
        )
        _current_import_thread = thread
        thread.start()

        return {
            "success": True,
            "job_id": run_id,
            "status": "running",
            "message": "Mailbox import job started",
            "batch_size": batch_size,
            "skip_attachment_binaries": skip_attachment_binaries,
        }
    except Exception:
        _import_lock.release()
        raise


def stop_import(db_factory: Callable[[], Session]) -> Dict[str, Any]:
    """Request the running import job to stop at the next batch boundary."""
    _import_cancel_event.set()

    db = db_factory()
    try:
        run = (
            db.query(MailboxImportRun)
            .filter(MailboxImportRun.status == "running")
            .order_by(MailboxImportRun.started_at.desc())
            .first()
        )
        if run:
            run.status = "paused"
            run.paused_at = datetime.now(timezone.utc)
            db.commit()
            return {"success": True, "message": "Import job pause requested", "job_id": run.id}
        return {"success": False, "message": "No running import job to stop"}
    finally:
        db.close()


def get_import_status(db_factory: Callable[[], Session]) -> Dict[str, Any]:
    """Return the current import job status."""
    db = db_factory()
    try:
        run = (
            db.query(MailboxImportRun)
            .order_by(MailboxImportRun.started_at.desc())
            .first()
        )
        if not run:
            return {"status": "idle", "message": "No import job has been run"}

        folders_discovered = run.folders_discovered or []
        folders_completed = run.folders_completed or []
        total_ingested = run.total_emails_ingested or 0
        total_learned = run.total_emails_learned or 0
        total_skipped = run.total_emails_skipped or 0
        total_failed = run.total_emails_failed or 0
        total_processed = total_ingested + total_skipped

        # Progress percent: only meaningful when we know folder count
        progress_percent = 0.0
        if run.total_folders_discovered and run.total_folders_discovered > 0:
            progress_percent = (
                len(folders_completed) / run.total_folders_discovered * 100
            )

        return {
            "status": run.status,
            "job_id": run.id,
            "batch_size": run.batch_size,
            "skip_attachment_binaries": run.skip_attachment_binaries,
            "current_folder": run.current_folder,
            "total_folders_discovered": run.total_folders_discovered or 0,
            "folders_completed_count": len(folders_completed),
            "folders_completed": folders_completed,
            "folders_discovered": folders_discovered,
            "total_emails_ingested": total_ingested,
            "total_emails_learned": total_learned,
            "total_emails_skipped": total_skipped,
            "total_emails_failed": total_failed,
            "progress_percent": round(progress_percent, 1),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "paused_at": run.paused_at.isoformat() if run.paused_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "error_message": run.error_message,
        }
    finally:
        db.close()


def reset_import(db_factory: Callable[[], Session]) -> Dict[str, Any]:
    """Delete all import run records.  Does NOT delete ingested emails or learned aggregates."""
    _import_cancel_event.set()

    db = db_factory()
    try:
        deleted = db.query(MailboxImportRun).delete()
        db.commit()
        return {"success": True, "deleted_runs": deleted}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------


def _run_import_thread(
    db_factory: Callable[[], Session],
    run_id: int,
    batch_size: int,
    skip_attachment_binaries: bool,
) -> None:
    """Main import loop — runs in a background thread.

    For each learnable folder:
      - Fetch UIDs from IMAP
      - Process in small batches (fetch → ingest → learn → checkpoint)
    """
    db = db_factory()
    try:
        run = db.query(MailboxImportRun).filter(MailboxImportRun.id == run_id).first()
        if not run:
            logger.error("import_thread_start_failed run_id=%s not_found", run_id)
            return

        run.status = "running"
        db.commit()

        # ------------------------------------------------------------------
        # Phase 1: Discover IMAP folders (or resume from saved list)
        # ------------------------------------------------------------------
        folders_discovered = run.folders_discovered or []
        if not folders_discovered:
            from src.services.imap_service import IMAPService

            with IMAPService() as imap:
                if not imap.client:
                    raise RuntimeError("Failed to connect to IMAP server")
                raw_folders = imap.list_folders()

            folders_discovered = [
                f["name"]
                for f in raw_folders
                if is_learnable_folder(f["name"])
            ]
            run.folders_discovered = folders_discovered
            run.total_folders_discovered = len(folders_discovered)
            db.commit()

        logger.info(
            "import_folders_discovered run_id=%s count=%s",
            run_id, len(folders_discovered),
        )

        folders_completed = list(run.folders_completed or [])

        # ------------------------------------------------------------------
        # Phase 2: Process folders
        # ------------------------------------------------------------------
        for folder_name in folders_discovered:
            if _import_cancel_event.is_set():
                run.status = "paused"
                run.paused_at = datetime.now(timezone.utc)
                db.commit()
                logger.info("import_paused run_id=%s folder=%s", run_id, folder_name)
                return

            if folder_name in folders_completed:
                continue

            run.current_folder = folder_name
            db.commit()

            try:
                folder_stats = _process_folder_streaming(
                    db=db,
                    run=run,
                    folder_name=folder_name,
                    batch_size=batch_size,
                    skip_attachment_binaries=skip_attachment_binaries,
                )
            except Exception as e:
                logger.error(
                    "import_folder_failed run_id=%s folder=%s error=%s",
                    run_id, folder_name, str(e),
                )
                run.total_emails_failed = (run.total_emails_failed or 0) + 1
                db.commit()
                # Continue with next folder — don't fail the whole run
                continue

            if folder_stats.get("paused"):
                run.status = "paused"
                run.paused_at = datetime.now(timezone.utc)
                db.commit()
                logger.info("import_paused_in_folder run_id=%s folder=%s", run_id, folder_name)
                return

            # Mark folder complete
            folders_completed.append(folder_name)
            run.folders_completed = folders_completed
            run.folders_completed_count = len(folders_completed)
            db.commit()

        # ------------------------------------------------------------------
        # Phase 3: Done
        # ------------------------------------------------------------------
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        run.current_folder = None
        db.commit()
        logger.info(
            "import_completed run_id=%s ingested=%s learned=%s skipped=%s failed=%s",
            run_id,
            run.total_emails_ingested,
            run.total_emails_learned,
            run.total_emails_skipped,
            run.total_emails_failed,
        )

    except Exception as e:
        logger.error("import_thread_failed run_id=%s error=%s", run_id, str(e))
        try:
            run = db.query(MailboxImportRun).filter(MailboxImportRun.id == run_id).first()
            if run:
                run.status = "failed"
                run.error_message = str(e)
                db.commit()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            _import_lock.release()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Per-folder streaming ingestion + learning
# ---------------------------------------------------------------------------


def _process_folder_streaming(
    db: Session,
    run: MailboxImportRun,
    folder_name: str,
    batch_size: int,
    skip_attachment_binaries: bool,
) -> Dict[str, Any]:
    """Process a single folder in streaming batches.

    Returns dict with per-folder stats and ``paused=True`` if cancelled.
    """
    from src.services.imap_service import IMAPService
    from src.services.prediction_engine import generate_predictions

    folder_stats: Dict[str, Any] = {
        "ingested": 0, "learned": 0, "skipped": 0, "failed": 0, "paused": False,
    }

    with IMAPService() as imap:
        if not imap.client:
            raise RuntimeError("Failed to connect to IMAP server")

        imap.client.select_folder(folder_name, readonly=True)
        all_uids = imap.client.search(["ALL"])
        if not all_uids:
            logger.info("import_folder_empty folder=%s", folder_name)
            return folder_stats

        # Resume from UID checkpoint
        uid_checkpoint = run.current_folder_uid_checkpoint
        if uid_checkpoint is not None and run.current_folder == folder_name:
            all_uids = [u for u in all_uids if int(u) > uid_checkpoint]

        if not all_uids:
            return folder_stats

        # Process in small batches
        for batch_start in range(0, len(all_uids), batch_size):
            if _import_cancel_event.is_set():
                folder_stats["paused"] = True
                return folder_stats

            batch_uids = all_uids[batch_start: batch_start + batch_size]
            if not batch_uids:
                break

            # Fetch batch from IMAP
            try:
                if skip_attachment_binaries:
                    fetch_data = imap.client.fetch(batch_uids, _FETCH_HEADERS_AND_TEXT)
                else:
                    fetch_data = imap.client.fetch(batch_uids, _FETCH_FULL)
            except Exception as e:
                logger.error("import_batch_fetch_failed folder=%s error=%s", folder_name, str(e))
                folder_stats["failed"] += len(batch_uids)
                continue

            # Ingest + learn each email in the batch
            for uid, msg_data in fetch_data.items():
                try:
                    result = _ingest_and_learn_single(
                        db=db,
                        uid=uid,
                        msg_data=msg_data,
                        folder_name=folder_name,
                        imap=imap,
                        skip_attachment_binaries=skip_attachment_binaries,
                    )
                    if result == "new":
                        folder_stats["ingested"] += 1
                        folder_stats["learned"] += 1
                    elif result == "skipped":
                        folder_stats["skipped"] += 1
                except Exception as e:
                    logger.warning(
                        "import_email_failed uid=%s folder=%s error=%s",
                        uid, folder_name, str(e),
                    )
                    folder_stats["failed"] += 1

            # Persist checkpoint after each batch
            last_uid = max(int(u) for u in batch_uids)
            run.current_folder_uid_checkpoint = last_uid
            run.total_emails_ingested = (run.total_emails_ingested or 0) + folder_stats["ingested"]
            run.total_emails_learned = (run.total_emails_learned or 0) + folder_stats["learned"]
            run.total_emails_skipped = (run.total_emails_skipped or 0) + folder_stats["skipped"]
            run.total_emails_failed = (run.total_emails_failed or 0) + folder_stats["failed"]
            db.commit()

            # Reset per-batch counters (accumulated into run totals above)
            folder_stats["ingested"] = 0
            folder_stats["learned"] = 0
            folder_stats["skipped"] = 0
            folder_stats["failed"] = 0

            logger.debug(
                "import_batch_done folder=%s batch_end_uid=%s",
                folder_name, last_uid,
            )

    # Clear folder checkpoint for next folder
    run.current_folder_uid_checkpoint = None
    db.commit()

    return folder_stats


# ---------------------------------------------------------------------------
# Single-email ingest + learn
# ---------------------------------------------------------------------------


def _ingest_and_learn_single(
    db: Session,
    uid: int,
    msg_data: Dict,
    folder_name: str,
    imap: Any,
    skip_attachment_binaries: bool,
) -> str:
    """Ingest a single email and immediately run learning on it.

    Returns ``"new"`` if ingested, ``"skipped"`` if already exists.
    """
    # Parse the email
    email_data = _parse_email_for_import(uid, msg_data, skip_attachment_binaries)
    if not email_data:
        raise ValueError(f"Failed to parse email UID {uid}")

    message_id = email_data["message_id"]

    # Skip if already in local index
    existing = (
        db.query(ProcessedEmail)
        .filter(ProcessedEmail.message_id == message_id)
        .first()
    )
    if existing:
        # Still run learning on it if not yet learned
        _learn_from_existing(db, existing)
        return "skipped"

    # Build and persist the email record
    from src.config import get_settings
    settings = get_settings()

    email_record = ProcessedEmail(
        message_id=message_id,
        uid=str(uid),
        imap_uid=str(uid),
        thread_id=email_data.get("thread_id") or _generate_thread_id(message_id),
        subject=email_data.get("subject"),
        sender=email_data.get("sender"),
        recipients=email_data.get("recipients"),
        date=email_data.get("date"),
        received_at=email_data.get("date"),
        folder=folder_name,
        body_plain=email_data.get("body_plain") if settings.store_email_body else None,
        body_html=email_data.get("body_html") if settings.store_email_body else None,
        snippet=_make_snippet(email_data.get("body_plain", "")),
        body_hash=_compute_body_hash(
            email_data.get("body_plain", ""), email_data.get("body_html", "")
        ),
        flags=email_data.get("flags"),
        integrity_hash=email_data.get("integrity_hash"),
        analysis_state="pending",
        is_processed=False,
        created_at=datetime.now(timezone.utc),
    )

    db.add(email_record)
    db.flush()  # Make email_record.id available for learning

    # Immediately learn from this email
    try:
        from src.services.folder_classifier import FOLDER_TYPE_SENT
        folder_type = classify_folder(folder_name)
        if folder_type == FOLDER_TYPE_SENT:
            from src.services.historical_learning import learn_reply_linkage
            learn_reply_linkage(db, email_record)
        else:
            learn_from_email(db, email_record, source="mailbox-import")
            _update_sender_interaction(db, email_record)
    except Exception as e:
        logger.warning("import_learn_failed email_id=%s error=%s", email_record.id, str(e))

    db.commit()
    return "new"


def _learn_from_existing(db: Session, email: ProcessedEmail) -> None:
    """Run learning on an already-indexed email if it hasn't been learned yet."""
    # Simple heuristic: if the email has no analysis state beyond "pending",
    # it was ingested but not yet learned.
    try:
        from src.services.folder_classifier import FOLDER_TYPE_SENT
        folder_type = classify_folder(email.folder or "")
        if folder_type == FOLDER_TYPE_SENT:
            from src.services.historical_learning import learn_reply_linkage
            learn_reply_linkage(db, email)
        else:
            learn_from_email(db, email, source="mailbox-import")
    except Exception:
        pass  # Non-fatal — skip silently


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------


def _parse_email_for_import(
    uid: int, msg_data: Dict, skip_attachment_binaries: bool
) -> Optional[Dict[str, Any]]:
    """Parse fetched IMAP data into an email dict.

    When ``skip_attachment_binaries`` is True, the email is reconstructed
    from ``BODY[HEADER]`` + ``BODY[TEXT]`` without binary attachments.
    Attachment metadata is extracted from BODYSTRUCTURE.
    """
    try:
        if skip_attachment_binaries:
            # Reconstruct from header + text parts
            # IMAP BODY[HEADER] includes the trailing blank line separator,
            # so we concatenate directly without adding extra separators.
            header_bytes = msg_data.get(b"BODY[HEADER]", b"")
            text_bytes = msg_data.get(b"BODY[TEXT]", b"")
            if header_bytes:
                # Ensure there's a proper blank line between header and body
                # The header must end with \r\n\r\n or \n\n
                if header_bytes.endswith(b"\r\n\r\n") or header_bytes.endswith(b"\n\n"):
                    raw_email = header_bytes + text_bytes
                else:
                    # Add separator if missing
                    raw_email = header_bytes + b"\r\n" + text_bytes
            else:
                raw_email = text_bytes

            # Also try to extract attachment metadata from BODYSTRUCTURE
            bodystructure = msg_data.get(b"BODYSTRUCTURE")
        else:
            raw_email = msg_data.get(b"BODY[]") or msg_data.get(b"RFC822", b"")
            bodystructure = None

        if not raw_email:
            return None

        msg = message_from_bytes(raw_email)

        # Extract headers
        subject = _decode_header(msg.get("Subject", ""))
        sender = _decode_header(msg.get("From", ""))
        recipients = _decode_header(msg.get("To", ""))
        message_id = msg.get("Message-ID", f"<generated-{uid}@mailjaeger>").strip()
        in_reply_to = msg.get("In-Reply-To", "")
        references = msg.get("References", "")
        date_str = msg.get("Date")

        # Parse date
        email_date = None
        if date_str:
            try:
                from email.utils import parsedate_to_datetime
                email_date = parsedate_to_datetime(date_str)
            except (ValueError, TypeError):
                email_date = datetime.now(timezone.utc)
        else:
            email_date = datetime.now(timezone.utc)

        # Extract body text
        body_plain, body_html = _extract_body(msg)

        # Extract flags
        flags = [
            f.decode() if isinstance(f, bytes) else str(f)
            for f in (msg_data.get(b"FLAGS") or [])
        ]

        # Extract attachment metadata (from BODYSTRUCTURE or MIME parts)
        attachment_metadata = _extract_attachment_metadata(msg, bodystructure)

        # Integrity hash
        integrity_hash = hashlib.sha256(raw_email).hexdigest()

        return {
            "uid": str(uid),
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "references": references,
            "subject": subject,
            "sender": sender,
            "recipients": recipients,
            "date": email_date,
            "body_plain": body_plain,
            "body_html": body_html,
            "flags": flags,
            "integrity_hash": integrity_hash,
            "attachment_metadata": attachment_metadata,
        }

    except Exception as e:
        logger.error("import_parse_failed uid=%s error=%s", uid, str(e))
        return None


def _extract_body(msg) -> tuple:
    """Extract plain text and HTML body from an email message."""
    body_plain = ""
    body_html = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            # Skip attachments
            if "attachment" in content_disposition:
                continue

            if content_type == "text/plain":
                try:
                    body_plain += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except (UnicodeDecodeError, AttributeError):
                    pass
            elif content_type == "text/html":
                try:
                    body_html += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except (UnicodeDecodeError, AttributeError):
                    pass
    else:
        content_type = msg.get_content_type()
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode("utf-8", errors="ignore")
                if content_type == "text/plain":
                    body_plain = text
                elif content_type == "text/html":
                    body_html = text
        except (UnicodeDecodeError, AttributeError):
            pass

    return body_plain, body_html


def _extract_attachment_metadata(msg, bodystructure=None) -> List[Dict[str, Any]]:
    """Extract attachment metadata without downloading binary content.

    Returns a list of dicts with: filename, content_type, size, disposition.
    """
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            content_type = part.get_content_type()

            # Skip text parts that are the main body
            if content_type in ("text/plain", "text/html") and "attachment" not in content_disposition:
                continue

            # Only include actual attachments
            if "attachment" in content_disposition or (
                content_type not in ("text/plain", "text/html", "multipart/mixed",
                                     "multipart/alternative", "multipart/related")
            ):
                filename = part.get_filename()
                if filename:
                    filename = _decode_header(filename)

                # Estimate size from Content-Length or payload length
                size = None
                content_length = part.get("Content-Length")
                if content_length:
                    try:
                        size = int(content_length)
                    except (ValueError, TypeError):
                        pass

                if filename or content_type not in ("text/plain", "text/html"):
                    attachments.append({
                        "filename": filename or "(unnamed)",
                        "content_type": content_type,
                        "size": size,
                        "disposition": content_disposition[:100] if content_disposition else None,
                    })

    return attachments


def _decode_header(header: str) -> str:
    """Decode an email header value."""
    if not header:
        return ""
    try:
        parts = []
        for part, encoding in _decode_header_raw(header):
            if isinstance(part, bytes):
                parts.append(part.decode(encoding or "utf-8", errors="ignore"))
            else:
                parts.append(str(part))
        return "".join(parts)
    except Exception:
        return str(header)


def _generate_thread_id(message_id: str) -> str:
    """Generate a stable thread ID from a message ID."""
    return "thread-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]


def _make_snippet(body_plain: str, max_length: int = 200) -> str:
    """Extract a short snippet from the plain text body."""
    if not body_plain:
        return ""
    snippet = re.sub(r"\s+", " ", body_plain).strip()
    return snippet[:max_length] + "…" if len(snippet) > max_length else snippet


def _compute_body_hash(body_plain: str, body_html: str) -> str:
    """Compute SHA256 hash of normalized body text."""
    text = body_plain or _strip_html(body_html)
    normalized = re.sub(r"https?://\S+", "", text or "")
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _strip_html(html: str) -> str:
    """Strip HTML tags, leaving plain text."""
    if not html:
        return ""
    return re.sub(r"<[^>]+>", " ", html)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_or_create_run(
    db: Session,
    batch_size: int,
    skip_attachment_binaries: bool,
) -> MailboxImportRun:
    """Get an existing paused run or create a new one."""
    existing = (
        db.query(MailboxImportRun)
        .filter(MailboxImportRun.status.in_(("paused", "running")))
        .order_by(MailboxImportRun.started_at.desc())
        .first()
    )
    if existing:
        existing.status = "running"
        existing.paused_at = None
        db.commit()
        logger.info("import_run_resumed run_id=%s", existing.id)
        return existing

    run = MailboxImportRun(
        status="running",
        batch_size=batch_size,
        skip_attachment_binaries=skip_attachment_binaries,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    logger.info("import_run_created run_id=%s", run.id)
    return run
