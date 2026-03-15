"""
Mail Ingestion Service — Priority 1, 3, 4

Imports emails from IMAP into the local mail index:
  IMAP → Mail Ingestion Service → Local Mail Index → Processing Pipeline → LLM only when needed

Responsibilities:
  - Connect to IMAP and fetch emails
  - Store metadata in the local database
  - Detect new messages (avoid duplicate imports)
  - Reconstruct email threads (Priority 3)
  - Compute body hashes for deduplication (Priority 4)
  - Track ingestion progress for pause/resume (Priority 7)
"""

import hashlib
import re
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy.orm import Session

from src.config import get_settings
from src.models.database import ProcessedEmail, AnalysisProgress
from src.services.imap_service import IMAPService
from src.utils.logging import get_logger
from src.utils.error_handling import sanitize_error

logger = get_logger(__name__)

# Analysis pipeline version — increment when logic changes to trigger re-analysis
PIPELINE_VERSION = "1.0.0"


class MailIngestionService:
    """
    Service responsible for importing emails from IMAP into the local mail index.

    Emails are fetched once and stored locally so the rest of the pipeline
    can operate without repeatedly hitting the mail server.
    """

    def __init__(self, db_session: Session):
        self.settings = get_settings()
        self.db = db_session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_folder(
        self,
        folder: str = "INBOX",
        max_emails: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Ingest emails from an IMAP folder into the local mail index.

        Returns a summary dict with counts of new, skipped, and failed emails.
        Supports pause/resume via the analysis_progress table.
        """
        effective_max = max_emails or self.settings.max_emails_per_batch
        run_id = run_id or str(uuid.uuid4())

        progress = self._get_or_create_progress(run_id, "ingestion", folder)
        stats = {"new": 0, "skipped": 0, "failed": 0, "total": 0}

        logger.info(
            f"Starting ingestion: folder={folder}, max={effective_max}, run_id={run_id}"
        )

        try:
            with IMAPService() as imap:
                imap.client.select_folder(folder)
                message_uids = imap.client.search(["ALL"])

                if not message_uids:
                    logger.info(f"No messages found in {folder}")
                    self._mark_progress_complete(progress, stats)
                    return stats

                # Apply resource budget limit
                if len(message_uids) > effective_max:
                    message_uids = message_uids[:effective_max]

                stats["total"] = len(message_uids)
                logger.info(f"Found {stats['total']} messages to check in {folder}")

                # Fetch in batches to control memory
                batch_size = min(self.settings.max_emails_per_batch, 25)
                for batch_start in range(0, len(message_uids), batch_size):
                    # Check if we should pause (resource limits)
                    if self._should_pause(progress, stats):
                        logger.info(f"Ingestion paused at {stats['new']} new emails")
                        self._mark_progress_paused(progress, stats, "resource_limit")
                        return stats

                    batch = message_uids[batch_start : batch_start + batch_size]

                    try:
                        fetch_data = imap.client.fetch(batch, [b"BODY.PEEK[]", b"FLAGS"])
                    except Exception as e:
                        sanitized = sanitize_error(e, debug=self.settings.debug)
                        logger.error(f"Batch fetch failed: {sanitized}")
                        stats["failed"] += len(batch)
                        continue

                    for uid, message_data in fetch_data.items():
                        try:
                            result = self._process_fetched_message(
                                uid, message_data, folder, imap
                            )
                            if result == "new":
                                stats["new"] += 1
                            elif result == "skipped":
                                stats["skipped"] += 1
                        except Exception as e:
                            sanitized = sanitize_error(e, debug=self.settings.debug)
                            logger.error(f"Failed to process UID {uid}: {sanitized}")
                            stats["failed"] += 1
                            continue

                    # Update progress checkpoint after each batch
                    if batch:
                        progress.last_email_id = batch[-1]
                        progress.processed_count = stats["new"] + stats["skipped"]
                        progress.failed_count = stats["failed"]
                        self.db.commit()

        except RuntimeError as e:
            sanitized = sanitize_error(e, debug=self.settings.debug)
            logger.error(f"IMAP connection failed during ingestion: {sanitized}")
            stats["failed"] = stats.get("total", 0) - stats.get("new", 0) - stats.get("skipped", 0)
            self._mark_progress_failed(progress, str(e))
            return stats

        self._mark_progress_complete(progress, stats)
        logger.info(
            f"Ingestion complete: {stats['new']} new, {stats['skipped']} skipped, "
            f"{stats['failed']} failed"
        )
        return stats

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_fetched_message(
        self,
        uid: int,
        message_data: Dict,
        folder: str,
        imap: IMAPService,
    ) -> str:
        """
        Process a single fetched message.

        Returns 'new', 'skipped', or raises an exception.
        """
        raw_email = message_data.get(b"BODY[]") or message_data.get(b"RFC822")
        if not raw_email:
            raise ValueError(f"No raw email data for UID {uid}")

        # Parse to extract message-id for duplicate check
        from email import message_from_bytes
        msg = message_from_bytes(raw_email)
        message_id = msg.get("Message-ID", f"<generated-{uid}@mailjaeger>").strip()

        # Skip if already in local index
        existing = (
            self.db.query(ProcessedEmail)
            .filter(ProcessedEmail.message_id == message_id)
            .first()
        )
        if existing:
            return "skipped"

        # Parse full email via the IMAP service parser
        email_data = imap._parse_email(uid, message_data)
        if not email_data:
            raise ValueError(f"Failed to parse email UID {uid}")

        # Compute body hash for deduplication (Priority 4)
        body_hash = self._compute_body_hash(
            email_data.get("body_plain", ""),
            email_data.get("body_html", ""),
        )

        # Reconstruct thread ID (Priority 3)
        thread_id = self._resolve_thread_id(
            message_id=message_id,
            in_reply_to=email_data.get("in_reply_to", ""),
            references=email_data.get("references", ""),
        )

        # Extract flags from message_data
        flags = [
            f.decode() if isinstance(f, bytes) else str(f)
            for f in (message_data.get(b"FLAGS") or [])
        ]

        # Build snippet
        snippet = self._make_snippet(email_data.get("body_plain", ""))

        # Persist to local index
        email_record = ProcessedEmail(
            message_id=message_id,
            uid=str(uid),
            imap_uid=str(uid),
            thread_id=thread_id,
            subject=email_data.get("subject"),
            sender=email_data.get("sender"),
            recipients=email_data.get("recipients"),
            date=email_data.get("date"),
            received_at=email_data.get("date"),
            folder=folder,
            body_plain=(
                email_data.get("body_plain") if self.settings.store_email_body else None
            ),
            body_html=(
                email_data.get("body_html") if self.settings.store_email_body else None
            ),
            snippet=snippet,
            body_hash=body_hash,
            flags=flags,
            integrity_hash=email_data.get("integrity_hash"),
            analysis_state="pending",
            analysis_version=PIPELINE_VERSION,
            is_processed=False,
            created_at=datetime.utcnow(),
        )

        self.db.add(email_record)
        self.db.commit()
        return "new"

    # ------------------------------------------------------------------
    # Thread reconstruction (Priority 3)
    # ------------------------------------------------------------------

    def _resolve_thread_id(
        self,
        message_id: str,
        in_reply_to: str,
        references: str,
    ) -> str:
        """
        Resolve or create a thread ID for this email.

        Algorithm:
        1. If In-Reply-To is set, look for the parent email in the DB.
           If found → use its thread_id.
        2. If References header contains known message IDs, use the earliest
           known thread_id.
        3. Otherwise, generate a new thread_id from the message_id.

        This enables thread-aware analysis later.
        """
        # 1. Check In-Reply-To
        if in_reply_to:
            parent_id = in_reply_to.strip()
            parent = (
                self.db.query(ProcessedEmail)
                .filter(ProcessedEmail.message_id == parent_id)
                .first()
            )
            if parent and parent.thread_id:
                return parent.thread_id

        # 2. Check References header (space-separated list of message IDs)
        if references:
            ref_ids = references.split()
            for ref_id in ref_ids:
                ref_email = (
                    self.db.query(ProcessedEmail)
                    .filter(ProcessedEmail.message_id == ref_id.strip())
                    .first()
                )
                if ref_email and ref_email.thread_id:
                    return ref_email.thread_id

        # 3. Create a new thread_id derived from the message_id
        return self._generate_thread_id(message_id)

    def _generate_thread_id(self, message_id: str) -> str:
        """Generate a stable thread ID from a message ID."""
        return "thread-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------
    # Body hash computation (Priority 4)
    # ------------------------------------------------------------------

    def _compute_body_hash(self, body_plain: str, body_html: str) -> str:
        """
        Compute a SHA256 hash of the normalized email body.

        Normalization:
        - Prefer plain text; fall back to HTML-stripped text
        - Remove HTML tags and tracking pixels
        - Collapse whitespace
        - Strip leading/trailing whitespace

        Purpose: Detect duplicate email bodies and reuse previous analysis.
        """
        text = body_plain or self._strip_html(body_html)
        normalized = self._normalize_body(text)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _strip_html(self, html: str) -> str:
        """Strip HTML tags, leaving plain text."""
        if not html:
            return ""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "img", "link"]):
                tag.decompose()
            return soup.get_text(separator=" ")
        except Exception:
            # Fallback: simple regex strip
            return re.sub(r"<[^>]+>", " ", html)

    def _normalize_body(self, text: str) -> str:
        """Normalize body text for consistent hashing."""
        if not text:
            return ""
        # Remove URLs (tracking pixels, unsubscribe links, etc.)
        text = re.sub(r"https?://\S+", "", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text)
        return text.strip().lower()

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def _make_snippet(self, body_plain: str, max_length: int = 200) -> str:
        """Extract a short snippet from the plain text body."""
        if not body_plain:
            return ""
        # Collapse whitespace and take the first max_length chars
        snippet = re.sub(r"\s+", " ", body_plain).strip()
        if len(snippet) > max_length:
            snippet = snippet[:max_length] + "…"
        return snippet

    # ------------------------------------------------------------------
    # Progress tracking (Priority 7)
    # ------------------------------------------------------------------

    def _get_or_create_progress(
        self, run_id: str, stage: str, folder: str
    ) -> AnalysisProgress:
        """Get or create an AnalysisProgress record for this run."""
        progress = (
            self.db.query(AnalysisProgress)
            .filter(
                AnalysisProgress.run_id == run_id,
                AnalysisProgress.stage == stage,
            )
            .first()
        )
        if not progress:
            progress = AnalysisProgress(
                run_id=run_id,
                stage=f"{stage}:{folder}",
                status="running",
                started_at=datetime.utcnow(),
            )
            self.db.add(progress)
            self.db.commit()
        return progress

    def _should_pause(
        self, progress: AnalysisProgress, stats: Dict[str, int]
    ) -> bool:
        """Check if processing should pause based on resource limits."""
        processed = stats.get("new", 0) + stats.get("skipped", 0)
        if processed > 0 and processed % self.settings.max_emails_per_batch == 0:
            elapsed = (datetime.utcnow() - progress.started_at).total_seconds() / 60
            if elapsed > self.settings.max_runtime_minutes:
                return True
        return False

    def _mark_progress_paused(
        self,
        progress: AnalysisProgress,
        stats: Dict[str, int],
        reason: str,
    ) -> None:
        progress.status = "paused"
        progress.paused_at = datetime.utcnow()
        progress.paused_reason = reason
        progress.processed_count = stats.get("new", 0) + stats.get("skipped", 0)
        self.db.commit()

    def _mark_progress_complete(
        self, progress: AnalysisProgress, stats: Dict[str, int]
    ) -> None:
        progress.status = "completed"
        progress.completed_at = datetime.utcnow()
        progress.processed_count = stats.get("new", 0) + stats.get("skipped", 0)
        progress.failed_count = stats.get("failed", 0)
        self.db.commit()

    def _mark_progress_failed(
        self, progress: AnalysisProgress, reason: str
    ) -> None:
        progress.status = "failed"
        progress.paused_reason = reason
        progress.completed_at = datetime.utcnow()
        self.db.commit()

    # ------------------------------------------------------------------
    # Body hash deduplication lookup (Priority 4)
    # ------------------------------------------------------------------

    def find_existing_analysis_by_body_hash(
        self, body_hash: str
    ) -> Optional[ProcessedEmail]:
        """
        Look up a previously analysed email with the same body hash.

        If found, the caller can reuse that email's analysis results
        rather than calling the LLM again.
        """
        return (
            self.db.query(ProcessedEmail)
            .filter(
                ProcessedEmail.body_hash == body_hash,
                ProcessedEmail.analysis_state == "deep_analyzed",
            )
            .order_by(ProcessedEmail.processed_at.desc())
            .first()
        )

    def get_ingestion_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get the current ingestion status for a run."""
        progress = (
            self.db.query(AnalysisProgress)
            .filter(AnalysisProgress.run_id == run_id)
            .first()
        )
        if not progress:
            return None
        return {
            "run_id": run_id,
            "stage": progress.stage,
            "status": progress.status,
            "processed_count": progress.processed_count,
            "total_count": progress.total_count,
            "failed_count": progress.failed_count,
            "last_email_id": progress.last_email_id,
            "started_at": progress.started_at.isoformat() if progress.started_at else None,
            "completed_at": progress.completed_at.isoformat() if progress.completed_at else None,
            "paused_reason": progress.paused_reason,
        }
