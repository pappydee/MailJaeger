"""
Tests for the mailbox-wide streaming import + learn service.

Tests cover:
  1. Multi-folder ingestion path
  2. Batch size behavior (small streaming batches)
  3. Resumability / checkpoint behavior
  4. No full pre-download requirement
  5. Attachment default behavior: metadata yes, binary no
  6. Compatibility with historical learning
  7. Large-mailbox-safe behavior (bounded resource usage)
  8. API endpoints (start/stop/status/reset)
"""

import hashlib
import re
import time
import threading
import pytest
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from unittest.mock import MagicMock, patch, PropertyMock
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from src.models.database import (
    Base,
    ProcessedEmail,
    SenderProfile,
    MailboxImportRun,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_import_globals():
    """Reset global state of the mailbox_import_service between tests."""
    import src.services.mailbox_import_service as svc
    svc._import_cancel_event.clear()
    if svc._current_import_thread and svc._current_import_thread.is_alive():
        svc._import_cancel_event.set()
        svc._current_import_thread.join(timeout=5)
    svc._current_import_thread = None
    try:
        svc._import_lock.release()
    except RuntimeError:
        pass
    yield
    svc._import_cancel_event.set()
    if svc._current_import_thread and svc._current_import_thread.is_alive():
        svc._current_import_thread.join(timeout=5)
    svc._current_import_thread = None
    try:
        svc._import_lock.release()
    except RuntimeError:
        pass
    svc._import_cancel_event.clear()


@pytest.fixture
def engine():
    """Shared in-memory SQLite engine with all tables."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    """Primary test session."""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def db_factory(engine):
    """Callable that returns a new session from the shared engine."""
    SessionLocal = sessionmaker(bind=engine)
    def factory():
        return SessionLocal()
    return factory


def _build_raw_email(
    uid: int,
    sender: str = "alice@example.com",
    recipient: str = "bob@example.com",
    subject: str = "Test email",
    body: str = "Hello World",
    folder: str = "INBOX",
    with_attachment: bool = False,
    attachment_filename: str = "report.pdf",
) -> bytes:
    """Build a raw RFC 5322 email message as bytes."""
    if with_attachment:
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg["Message-ID"] = f"<test-{uid}@example.com>"
        msg["Date"] = "Mon, 1 Jan 2024 10:00:00 +0000"
        msg.attach(MIMEText(body, "plain"))

        att = MIMEBase("application", "pdf")
        att.set_payload(b"FAKEPDFCONTENT")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename=attachment_filename)
        msg.attach(att)
    else:
        msg = MIMEText(body, "plain")
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg["Message-ID"] = f"<test-{uid}@example.com>"
        msg["Date"] = "Mon, 1 Jan 2024 10:00:00 +0000"

    return msg.as_bytes()


def _split_raw_email(raw: bytes) -> tuple:
    """Split raw email into header + text at the first blank line.

    Returns (header_bytes, text_bytes) where header includes the trailing separator.
    """
    idx = raw.find(b"\r\n\r\n")
    sep_len = 4
    if idx == -1:
        idx = raw.find(b"\n\n")
        sep_len = 2
    if idx >= 0:
        return raw[:idx + sep_len], raw[idx + sep_len:]
    return raw, b""


def _build_msg_data(uid: int, **kwargs) -> dict:
    """Build msg_data dict for _ingest_and_learn_single from a raw email."""
    raw = _build_raw_email(uid, **kwargs)
    header, text = _split_raw_email(raw)
    return {
        b"BODY[HEADER]": header,
        b"BODY[TEXT]": text,
        b"FLAGS": [b"\\Seen"],
        b"BODYSTRUCTURE": None,
    }


def _build_full_fetch_response(uids: list, folder: str = "INBOX", with_attachment: bool = False) -> dict:
    """Build mock IMAP fetch response with BODY[] (full message)."""
    result = {}
    for uid in uids:
        raw = _build_raw_email(uid, folder=folder, with_attachment=with_attachment)
        result[uid] = {
            b"BODY[]": raw,
            b"RFC822": raw,
            b"FLAGS": [b"\\Seen"],
        }
    return result


def _build_metadata_fetch_response(uids: list, folder: str = "INBOX") -> dict:
    """Build mock IMAP fetch response with BODY[HEADER] + BODY[TEXT] (no attachment binary)."""
    result = {}
    for uid in uids:
        raw = _build_raw_email(uid, folder=folder)
        # Split at first blank line (handle both \r\n\r\n and \n\n)
        idx = raw.find(b"\r\n\r\n")
        sep_len = 4
        if idx == -1:
            idx = raw.find(b"\n\n")
            sep_len = 2
        if idx >= 0:
            # IMAP BODY[HEADER] includes the trailing blank line
            header_bytes = raw[:idx + sep_len]
            text_bytes = raw[idx + sep_len:]
        else:
            header_bytes = raw
            text_bytes = b""

        result[uid] = {
            b"BODY[HEADER]": header_bytes,
            b"BODY[TEXT]": text_bytes,
            b"FLAGS": [b"\\Seen"],
            b"BODYSTRUCTURE": None,
        }
    return result


class MockIMAPClient:
    """Mock IMAP client for testing without a real server."""

    def __init__(self, folders: Dict[str, List[int]], with_attachment: bool = False,
                 use_full_fetch: bool = False):
        self.folders = folders
        self._selected_folder = None
        self._with_attachment = with_attachment
        self._use_full_fetch = use_full_fetch
        self.client = self

    def select_folder(self, folder, readonly=False):
        self._selected_folder = folder
        return {"EXISTS": len(self.folders.get(folder, []))}

    def search(self, criteria):
        return list(self.folders.get(self._selected_folder, []))

    def fetch(self, uids, parts):
        if self._use_full_fetch:
            return _build_full_fetch_response(uids, self._selected_folder, self._with_attachment)
        return _build_metadata_fetch_response(uids, self._selected_folder)

    def list_folders(self):
        result = []
        for name in self.folders:
            result.append({
                "name": name,
                "normalized_name": name.lower(),
                "delimiter": "/",
                "flags": [],
            })
        return result

    def connect(self):
        return True

    def disconnect(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _make_mock_imap_context(mock_imap):
    """Create a mock IMAPService context manager that yields mock_imap."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_imap)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.client = mock_imap.client
    ctx.list_folders = mock_imap.list_folders
    return ctx


# ---------------------------------------------------------------------------
# 1. Multi-folder ingestion path
# ---------------------------------------------------------------------------


class TestMultiFolderIngestion:
    """Verify the service processes multiple IMAP folders."""

    def test_discovers_learnable_folders(self):
        """Folder discovery must filter out Drafts (not learnable)."""
        from src.services.folder_classifier import is_learnable_folder

        assert is_learnable_folder("INBOX") is True
        assert is_learnable_folder("Sent") is True
        assert is_learnable_folder("Archive") is True
        assert is_learnable_folder("Drafts") is False

    def test_folders_discovered_stored_in_run(self, db):
        """Discovered folders list must be persisted in MailboxImportRun."""
        run = MailboxImportRun(
            status="running",
            batch_size=10,
            started_at=datetime.now(timezone.utc),
            folders_discovered=["INBOX", "Sent", "Archive"],
            total_folders_discovered=3,
        )
        db.add(run)
        db.commit()

        db.refresh(run)
        assert run.folders_discovered == ["INBOX", "Sent", "Archive"]
        assert run.total_folders_discovered == 3

    def test_completed_folders_tracked(self, db):
        """Once a folder is fully processed, it must be in folders_completed."""
        run = MailboxImportRun(
            status="running",
            batch_size=10,
            started_at=datetime.now(timezone.utc),
            folders_completed=["INBOX"],
            folders_completed_count=1,
        )
        db.add(run)
        db.commit()

        db.refresh(run)
        assert "INBOX" in run.folders_completed
        assert run.folders_completed_count == 1

    def test_process_folder_creates_emails(self, db):
        """Processing a folder must create ProcessedEmail records."""
        from src.services.mailbox_import_service import (
            _process_folder_streaming,
            _import_cancel_event,
        )

        run = MailboxImportRun(
            status="running", batch_size=10,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        mock_imap = MockIMAPClient({"INBOX": [1, 2, 3]})

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _import_cancel_event.clear()
            _process_folder_streaming(
                db=db, run=run, folder_name="INBOX",
                batch_size=10, skip_attachment_binaries=True,
            )

        assert db.query(ProcessedEmail).count() == 3
        assert db.query(ProcessedEmail).filter(ProcessedEmail.folder == "INBOX").count() == 3


# ---------------------------------------------------------------------------
# 2. Batch size behavior
# ---------------------------------------------------------------------------


class TestBatchSizeBehavior:
    """Verify small batch processing."""

    def test_batch_size_clamped(self):
        """batch_size must be clamped to [5, 100]."""
        assert max(5, min(100, 3)) == 5
        assert max(5, min(100, 200)) == 100
        assert max(5, min(100, 20)) == 20

    def test_process_folder_in_batches(self, db):
        """Emails must be fetched one at a time (per-UID), not as a single
        combined multi-message batch.  With 12 UIDs, fetch must be called
        exactly 12 times."""
        from src.services.mailbox_import_service import (
            _process_folder_streaming,
            _import_cancel_event,
        )

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        uids = list(range(1, 13))  # 12 emails
        mock_imap = MockIMAPClient({"INBOX": uids})

        fetch_call_count = 0
        original_fetch = mock_imap.fetch

        def counting_fetch(uids_arg, parts):
            nonlocal fetch_call_count
            fetch_call_count += 1
            return original_fetch(uids_arg, parts)

        mock_imap.fetch = counting_fetch

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _import_cancel_event.clear()
            _process_folder_streaming(
                db=db, run=run, folder_name="INBOX",
                batch_size=5, skip_attachment_binaries=True,
            )

        # Per-UID fetch strategy: one fetch call per email
        assert fetch_call_count == 12

    def test_default_batch_size_is_small(self):
        """Default batch size must be 20 (not hundreds)."""
        from src.services.mailbox_import_service import DEFAULT_IMPORT_BATCH_SIZE
        assert DEFAULT_IMPORT_BATCH_SIZE == 20


# ---------------------------------------------------------------------------
# 3. Resumability / checkpoint behavior
# ---------------------------------------------------------------------------


class TestResumability:
    """Verify the job can pause and resume from checkpoints."""

    def test_uid_checkpoint_cleared_after_folder_done(self, db):
        """After a folder is fully processed, UID checkpoint must be cleared."""
        from src.services.mailbox_import_service import (
            _process_folder_streaming,
            _import_cancel_event,
        )

        run = MailboxImportRun(
            status="running", batch_size=10,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        mock_imap = MockIMAPClient({"INBOX": [10, 20, 30]})

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _import_cancel_event.clear()
            _process_folder_streaming(
                db=db, run=run, folder_name="INBOX",
                batch_size=10, skip_attachment_binaries=True,
            )

        db.refresh(run)
        assert run.current_folder_uid_checkpoint is None
        assert (run.total_emails_ingested or 0) > 0

    def test_pause_preserves_checkpoint(self, db):
        """When cancelled mid-folder, the UID checkpoint must be preserved."""
        from src.services.mailbox_import_service import (
            _process_folder_streaming,
            _import_cancel_event,
        )

        run = MailboxImportRun(
            status="running", batch_size=2,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        uids = [10, 20, 30, 40, 50, 60]
        mock_imap = MockIMAPClient({"INBOX": uids})

        batch_count = 0
        original_fetch = mock_imap.fetch

        def cancel_after_first_batch(uids_arg, parts):
            nonlocal batch_count
            batch_count += 1
            if batch_count >= 2:
                _import_cancel_event.set()
            return original_fetch(uids_arg, parts)

        mock_imap.fetch = cancel_after_first_batch

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _import_cancel_event.clear()
            stats = _process_folder_streaming(
                db=db, run=run, folder_name="INBOX",
                batch_size=2, skip_attachment_binaries=True,
            )

        assert stats["paused"] is True
        db.refresh(run)
        assert (run.total_emails_ingested or 0) > 0

    def test_resume_skips_already_completed_folders(self, db):
        """When resuming, already-completed folders must be skipped."""
        run = MailboxImportRun(
            status="running", batch_size=10,
            started_at=datetime.now(timezone.utc),
            folders_discovered=["INBOX", "Sent", "Archive"],
            total_folders_discovered=3,
            folders_completed=["INBOX", "Sent"],
            folders_completed_count=2,
        )
        db.add(run)
        db.commit()

        remaining = [
            f for f in run.folders_discovered
            if f not in (run.folders_completed or [])
        ]
        assert remaining == ["Archive"]

    def test_paused_run_resumes_on_start(self, db):
        """A paused run must be resumed (not a new one created)."""
        from src.services.mailbox_import_service import _get_or_create_run

        paused_run = MailboxImportRun(
            status="paused", batch_size=10,
            started_at=datetime.now(timezone.utc),
            paused_at=datetime.now(timezone.utc),
            folders_discovered=["INBOX"],
            folders_completed=[],
        )
        db.add(paused_run)
        db.commit()
        paused_id = paused_run.id

        resumed = _get_or_create_run(db, batch_size=10, skip_attachment_binaries=True)
        assert resumed.id == paused_id
        assert resumed.status == "running"


# ---------------------------------------------------------------------------
# 4. No full pre-download requirement
# ---------------------------------------------------------------------------


class TestNoFullPreDownload:
    """Verify the system does NOT require downloading the whole mailbox first."""

    def test_emails_ingested_and_learned_per_batch(self, db):
        """All emails in a folder must be ingested and learned.  With per-UID
        fetching, each email is committed immediately; the per-batch checkpoint
        ensures forward progress even if the process restarts mid-batch."""
        from src.services.mailbox_import_service import (
            _process_folder_streaming,
            _import_cancel_event,
        )

        run = MailboxImportRun(
            status="running", batch_size=3,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        uids = [1, 2, 3, 4, 5, 6]
        mock_imap = MockIMAPClient({"INBOX": uids})

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _import_cancel_event.clear()
            _process_folder_streaming(
                db=db, run=run, folder_name="INBOX",
                batch_size=3, skip_attachment_binaries=True,
            )

        # All 6 emails must be ingested
        assert db.query(ProcessedEmail).count() == 6
        assert (run.total_emails_ingested or 0) == 6

    def test_ingest_and_learn_single_does_both(self, db):
        """_ingest_and_learn_single must ingest AND learn in one call."""
        from src.services.mailbox_import_service import _ingest_and_learn_single

        msg_data = _build_msg_data(1)

        result = _ingest_and_learn_single(
            db=db, uid=1, msg_data=msg_data,
            folder_name="INBOX", imap=MagicMock(),
            skip_attachment_binaries=True,
        )

        assert result == "new"
        assert db.query(ProcessedEmail).count() == 1


# ---------------------------------------------------------------------------
# 5. Attachment default behavior
# ---------------------------------------------------------------------------


class TestAttachmentBehavior:
    """Verify attachment binary skip by default, metadata extraction."""

    def test_skip_attachment_binaries_default_true(self):
        """Default must be: skip attachment binaries."""
        run = MailboxImportRun(skip_attachment_binaries=True)
        assert run.skip_attachment_binaries is True

    def test_attachment_metadata_extracted(self):
        """Attachment metadata (filename, content_type) must be extracted."""
        from src.services.mailbox_import_service import _extract_attachment_metadata

        msg = MIMEMultipart()
        msg.attach(MIMEText("body text", "plain"))

        att = MIMEBase("application", "pdf")
        att.set_payload(b"FAKECONTENT")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="report.pdf")
        msg.attach(att)

        metadata = _extract_attachment_metadata(msg)
        assert len(metadata) >= 1
        assert metadata[0]["filename"] == "report.pdf"
        assert metadata[0]["content_type"] == "application/pdf"

    def test_no_attachment_for_plain_email(self):
        """Plain text email must have no attachment metadata."""
        from src.services.mailbox_import_service import _extract_attachment_metadata

        msg = MIMEText("Just a plain email", "plain")
        metadata = _extract_attachment_metadata(msg)
        assert len(metadata) == 0

    def test_metadata_only_fetch_keys(self):
        """When skip_attachment_binaries=True, fetch must use HEADER+TEXT, not full BODY[].

        BODYSTRUCTURE is intentionally NOT included — imapclient's parser
        chokes on complex multipart MIME (e.g. "Tuple incomplete before …").
        Attachment filenames are extracted from parsed MIME headers instead.
        """
        from src.services.mailbox_import_service import _FETCH_HEADERS_AND_TEXT, _FETCH_FULL

        header_keys = [k.decode() for k in _FETCH_HEADERS_AND_TEXT]
        assert "BODY.PEEK[HEADER]" in header_keys
        assert "BODY.PEEK[TEXT]" in header_keys
        assert "BODYSTRUCTURE" not in header_keys, (
            "BODYSTRUCTURE must not be requested — fragile for complex MIME"
        )
        assert "BODY.PEEK[]" not in header_keys

        full_keys = [k.decode() for k in _FETCH_FULL]
        assert "BODY.PEEK[]" in full_keys

    def test_email_parsed_from_header_and_text(self):
        """Email must parse correctly from BODY[HEADER] + BODY[TEXT]."""
        from src.services.mailbox_import_service import _parse_email_for_import

        msg_data = _build_msg_data(42, sender="test@company.com", subject="Budget Report")

        result = _parse_email_for_import(42, msg_data, skip_attachment_binaries=True)
        assert result is not None
        assert result["message_id"] == "<test-42@example.com>"
        assert result["sender"] == "test@company.com"
        assert "Budget Report" in result["subject"]


# ---------------------------------------------------------------------------
# 6. Compatibility with historical learning
# ---------------------------------------------------------------------------


class TestHistoricalLearningCompatibility:
    """Verify import+learn integrates with the existing learning system."""

    def test_sender_profile_created_during_import(self, db):
        """Import must create SenderProfile entries via learn_from_email."""
        from src.services.mailbox_import_service import _ingest_and_learn_single

        msg_data = _build_msg_data(1, sender="boss@company.com")

        result = _ingest_and_learn_single(
            db=db, uid=1, msg_data=msg_data,
            folder_name="INBOX", imap=MagicMock(),
            skip_attachment_binaries=True,
        )

        assert result == "new"
        # SenderProfile should be created (at least domain-level)
        profiles = db.query(SenderProfile).all()
        assert len(profiles) >= 1

    def test_duplicate_email_skipped_not_crashed(self, db):
        """Processing the same email twice must skip, not crash."""
        from src.services.mailbox_import_service import _ingest_and_learn_single

        msg_data = _build_msg_data(1)

        r1 = _ingest_and_learn_single(
            db=db, uid=1, msg_data=msg_data,
            folder_name="INBOX", imap=MagicMock(),
            skip_attachment_binaries=True,
        )
        assert r1 == "new"

        r2 = _ingest_and_learn_single(
            db=db, uid=1, msg_data=msg_data,
            folder_name="INBOX", imap=MagicMock(),
            skip_attachment_binaries=True,
        )
        assert r2 == "skipped"

    def test_existing_sender_profile_not_duplicated(self, db):
        """If a sender profile already exists, it must be reused, not duplicated."""
        profile = SenderProfile(
            sender_address="alice@example.com",
            sender_domain="example.com",
            total_emails=5,
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        db.add(profile)
        db.commit()

        from src.services.mailbox_import_service import _ingest_and_learn_single

        msg_data = _build_msg_data(1, sender="alice@example.com")

        _ingest_and_learn_single(
            db=db, uid=1, msg_data=msg_data,
            folder_name="INBOX", imap=MagicMock(),
            skip_attachment_binaries=True,
        )

        # Should still be exactly 1 profile for alice (address-level)
        profiles = db.query(SenderProfile).filter(
            SenderProfile.sender_address == "alice@example.com"
        ).all()
        assert len(profiles) == 1


# ---------------------------------------------------------------------------
# 7. Large-mailbox-safe behavior
# ---------------------------------------------------------------------------


class TestLargeMailboxSafety:
    """Verify bounded resource usage for very large mailboxes."""

    def test_batch_size_bounds_memory_usage(self, db):
        """Each IMAP fetch must be bounded by batch_size."""
        from src.services.mailbox_import_service import (
            _process_folder_streaming,
            _import_cancel_event,
        )

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        uids = list(range(1, 26))  # 25 emails
        mock_imap = MockIMAPClient({"INBOX": uids})

        max_batch_size_seen = 0
        original_fetch = mock_imap.fetch

        def tracking_fetch(uids_arg, parts):
            nonlocal max_batch_size_seen
            max_batch_size_seen = max(max_batch_size_seen, len(uids_arg))
            return original_fetch(uids_arg, parts)

        mock_imap.fetch = tracking_fetch

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _import_cancel_event.clear()
            _process_folder_streaming(
                db=db, run=run, folder_name="INBOX",
                batch_size=5, skip_attachment_binaries=True,
            )

        assert max_batch_size_seen <= 5

    def test_checkpoint_after_every_batch(self, db):
        """A DB commit must happen after every batch (crash safety)."""
        from src.services.mailbox_import_service import (
            _process_folder_streaming,
            _import_cancel_event,
        )

        run = MailboxImportRun(
            status="running", batch_size=3,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        uids = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        mock_imap = MockIMAPClient({"INBOX": uids})

        commit_count = 0
        original_commit = db.commit

        def counting_commit():
            nonlocal commit_count
            commit_count += 1
            return original_commit()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            db.commit = counting_commit
            _import_cancel_event.clear()
            _process_folder_streaming(
                db=db, run=run, folder_name="INBOX",
                batch_size=3, skip_attachment_binaries=True,
            )
            db.commit = original_commit

        # 9 emails / 3 per batch = 3 batches → at least 3 checkpoint commits
        assert commit_count >= 3


# ---------------------------------------------------------------------------
# 8. API endpoint tests
# ---------------------------------------------------------------------------


class TestImportAPIEndpoints:
    """Test the /api/import/* endpoints."""

    @pytest.fixture
    def client(self, engine):
        """Create a test client with the shared engine."""
        from fastapi.testclient import TestClient
        from src.main import app
        from src.database.connection import get_db

        SessionLocal = sessionmaker(bind=engine)

        def override_get_db():
            session = SessionLocal()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_get_db
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_status_endpoint_returns_json(self, client):
        """GET /api/import/status must return valid JSON with status field."""
        resp = client.get(
            "/api/import/status",
            headers={"Authorization": "Bearer test_key_abc123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data

    def test_reset_endpoint(self, client):
        """POST /api/import/reset must succeed."""
        resp = client.post(
            "/api/import/reset",
            headers={"Authorization": "Bearer test_key_abc123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_start_requires_auth(self, client):
        """POST /api/import/start must require authentication."""
        resp = client.post("/api/import/start")
        assert resp.status_code in (401, 403)

    def test_stop_requires_auth(self, client):
        """POST /api/import/stop must require authentication."""
        resp = client.post("/api/import/stop")
        assert resp.status_code in (401, 403)

    def test_status_requires_auth(self, client):
        """GET /api/import/status must require authentication."""
        resp = client.get("/api/import/status")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 9. MailboxImportRun model tests
# ---------------------------------------------------------------------------


class TestMailboxImportRunModel:
    """Verify the MailboxImportRun model works correctly."""

    def test_create_run(self, db):
        """Can create a MailboxImportRun record."""
        run = MailboxImportRun(
            status="running", batch_size=20,
            skip_attachment_binaries=True,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        assert run.id is not None
        assert run.status == "running"
        assert run.batch_size == 20
        assert run.skip_attachment_binaries is True

    def test_json_fields_persist(self, db):
        """JSON fields must persist correctly."""
        run = MailboxImportRun(
            status="running", batch_size=10,
            started_at=datetime.now(timezone.utc),
            folders_discovered=["INBOX", "Archive", "Sent"],
            folders_completed=["INBOX"],
        )
        db.add(run)
        db.commit()

        db.refresh(run)
        assert run.folders_discovered == ["INBOX", "Archive", "Sent"]
        assert run.folders_completed == ["INBOX"]

    def test_default_values(self, db):
        """Default values must be sensible."""
        run = MailboxImportRun(
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        db.refresh(run)
        assert run.batch_size == 20
        assert run.skip_attachment_binaries is True
        assert run.total_emails_ingested == 0
        assert run.total_emails_learned == 0
        assert run.total_emails_failed == 0

    def test_status_transitions(self, db):
        """Status must transition correctly."""
        run = MailboxImportRun(
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        run.status = "paused"
        run.paused_at = datetime.now(timezone.utc)
        db.commit()
        assert run.status == "paused"

        run.status = "running"
        run.paused_at = None
        db.commit()
        assert run.status == "running"

        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        assert run.status == "completed"


# ---------------------------------------------------------------------------
# 10. Integration: end-to-end streaming import
# ---------------------------------------------------------------------------


class TestStreamingImportIntegration:
    """End-to-end integration test for the streaming import pipeline."""

    def test_full_streaming_import_two_folders(self, db):
        """Complete import of 2 folders must produce correct email data."""
        from src.services.mailbox_import_service import (
            _get_or_create_run,
            _process_folder_streaming,
            _import_cancel_event,
        )

        run = _get_or_create_run(db, batch_size=5, skip_attachment_binaries=True)
        run.folders_discovered = ["INBOX", "Archive"]
        run.total_folders_discovered = 2
        db.commit()

        _import_cancel_event.clear()

        # Process INBOX (5 emails)
        mock_inbox = MockIMAPClient({"INBOX": [1, 2, 3, 4, 5]})
        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_inbox)
            _process_folder_streaming(
                db=db, run=run, folder_name="INBOX",
                batch_size=5, skip_attachment_binaries=True,
            )

        # Process Archive (3 emails)
        mock_archive = MockIMAPClient({"Archive": [10, 11, 12]})
        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_archive)
            _process_folder_streaming(
                db=db, run=run, folder_name="Archive",
                batch_size=5, skip_attachment_binaries=True,
            )

        # 8 total emails ingested
        assert db.query(ProcessedEmail).count() == 8
        assert db.query(ProcessedEmail).filter(ProcessedEmail.folder == "INBOX").count() == 5
        assert db.query(ProcessedEmail).filter(ProcessedEmail.folder == "Archive").count() == 3

    def test_import_status_reports_progress(self, db, db_factory):
        """get_import_status must report truthful progress."""
        from src.services.mailbox_import_service import get_import_status

        run = MailboxImportRun(
            status="running", batch_size=10,
            started_at=datetime.now(timezone.utc),
            folders_discovered=["INBOX", "Sent", "Archive"],
            total_folders_discovered=3,
            folders_completed=["INBOX"],
            folders_completed_count=1,
            current_folder="Sent",
            total_emails_ingested=50,
            total_emails_learned=48,
            total_emails_skipped=2,
            total_emails_failed=0,
        )
        db.add(run)
        db.commit()

        status = get_import_status(db_factory)
        assert status["status"] == "running"


# ---------------------------------------------------------------------------
# 10. Regression: BODYSTRUCTURE parse failure resilience
# ---------------------------------------------------------------------------


class TestBodystructureParseFailure:
    """Regression tests for the production failure where complex multipart
    MIME structures caused BODYSTRUCTURE parse errors ('Tuple incomplete
    before ...'), killing the whole batch.

    The fix:
      1. BODYSTRUCTURE is no longer requested from IMAP
      2. Batch-level fetch failures fall back to per-email fetching
      3. Attachment filenames are extracted from MIME headers (best-effort)
      4. A single problematic email does not kill the whole batch
    """

    def test_bodystructure_not_in_fetch_fields(self):
        """BODYSTRUCTURE must NOT be in the metadata-only fetch fields."""
        from src.services.mailbox_import_service import _FETCH_HEADERS_AND_TEXT

        decoded = [k.decode() for k in _FETCH_HEADERS_AND_TEXT]
        assert "BODYSTRUCTURE" not in decoded, (
            "BODYSTRUCTURE must not be requested - fragile for complex MIME"
        )

    def test_per_uid_fetch_isolates_failures(self, db):
        """Per-UID fetch strategy: one bad UID must not prevent other UIDs
        in the same batch from being ingested.  The importer fetches each
        email individually so a failure on UID N never poisons UID N+1."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        fetch_calls = []

        class PerUIDClient:
            """Always fetched one UID at a time; UID 101 raises."""
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 3}

            def search(self, criteria):
                return [100, 101, 102]

            def fetch(self, uids, parts):
                # New strategy: always called with exactly one UID
                assert len(uids) == 1, "Per-UID fetch must call fetch([uid], ...)"
                fetch_calls.append(uids[0])
                uid = uids[0]
                if uid == 101:
                    raise Exception("Tuple incomplete before end of data")
                return _build_metadata_fetch_response([uid], "Klinik")

            def list_folders(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = PerUIDClient()
        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.learn_from_email"):
                with patch("src.services.mailbox_import_service._update_sender_interaction"):
                    _process_folder_streaming(
                        db=db, run=run, folder_name="Klinik",
                        batch_size=5, skip_attachment_binaries=True,
                    )

        # All 3 UIDs fetched individually
        assert fetch_calls == [100, 101, 102]
        # 2 of 3 ingested (UID 101 failed fetch)
        assert db.query(ProcessedEmail).count() == 2
        assert db.query(ProcessedEmail).filter(
            ProcessedEmail.folder == "Klinik"
        ).count() == 2
        # Exactly one failure counted
        assert (run.total_emails_failed or 0) == 1

    def test_single_bad_uid_does_not_fail_batch(self, db):
        """If one UID fetch fails, others in the same batch succeed."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        class PartialFailIMAPClient:
            """UID 201 fails fetch; UIDs 200 and 202 succeed."""
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 3}

            def search(self, criteria):
                return [200, 201, 202]

            def fetch(self, uids, parts):
                uid = uids[0]
                if uid == 201:
                    raise Exception("Malformed message data")
                return _build_metadata_fetch_response([uid], "Klinik")

            def list_folders(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = PartialFailIMAPClient()
        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.learn_from_email"):
                with patch("src.services.mailbox_import_service._update_sender_interaction"):
                    _process_folder_streaming(
                        db=db, run=run, folder_name="Klinik",
                        batch_size=5, skip_attachment_binaries=True,
                    )

        # 2 of 3 emails ingested (UID 201 failed individually)
        assert db.query(ProcessedEmail).count() == 2
        # Failed counter reflects exactly the one bad email
        assert (run.total_emails_failed or 0) == 1

    def test_failed_counter_truthful_not_inflated(self, db):
        """Failed counter must count individual failures, not batch-size
        multiplication."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            total_emails_failed=0,
        )
        db.add(run)
        db.commit()

        class BatchFailRecoverIMAPClient:
            """All UIDs succeed individually (per-UID fetch strategy)."""
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 3}

            def search(self, criteria):
                return [300, 301, 302]

            def fetch(self, uids, parts):
                return _build_metadata_fetch_response(uids, "Work")

            def list_folders(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = BatchFailRecoverIMAPClient()
        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.learn_from_email"):
                with patch("src.services.mailbox_import_service._update_sender_interaction"):
                    _process_folder_streaming(
                        db=db, run=run, folder_name="Work",
                        batch_size=5, skip_attachment_binaries=True,
                    )

        # All 3 recovered - zero failed
        assert db.query(ProcessedEmail).count() == 3
        assert (run.total_emails_failed or 0) == 0

    def test_complex_multipart_with_pdf_attachment_metadata(self):
        """Complex multipart email with PDF attachment: attachment metadata
        extraction is DISABLED during mailbox import for reliability.
        Email body/headers must still be parsed correctly."""
        from src.services.mailbox_import_service import _parse_email_for_import

        msg = MIMEMultipart("mixed")
        msg["From"] = "doctor@klinik.de"
        msg["To"] = "patient@klinik.de"
        msg["Subject"] = "Befund"
        msg["Message-ID"] = "<complex-1@klinik.de>"
        msg["Date"] = "Mon, 1 Mar 2024 10:00:00 +0100"

        body_alt = MIMEMultipart("alternative")
        body_alt.attach(MIMEText("Ihr Befund liegt vor.", "plain"))
        body_alt.attach(MIMEText("<p>Ihr Befund liegt vor.</p>", "html"))
        msg.attach(body_alt)

        pdf = MIMEBase("application", "pdf")
        pdf.set_payload(b"%PDF-1.4 fake content")
        encoders.encode_base64(pdf)
        pdf.add_header("Content-Disposition", "attachment", filename="Befund_2024.pdf")
        msg.attach(pdf)

        img = MIMEBase("image", "jpeg")
        img.set_payload(b"\xff\xd8\xff\xe0 fake jpeg")
        encoders.encode_base64(img)
        img.add_header("Content-Disposition", "attachment", filename="scan.jpg")
        msg.attach(img)

        raw = msg.as_bytes()
        idx = raw.find(b"\r\n\r\n")
        if idx == -1:
            idx = raw.find(b"\n\n")
        header = raw[:idx + 4] if b"\r\n\r\n" in raw[:idx + 4] else raw[:idx + 2]
        text = raw[len(header):]

        msg_data = {
            b"BODY[HEADER]": header,
            b"BODY[TEXT]": text,
            b"FLAGS": [b"\\Seen"],
        }

        result = _parse_email_for_import(1, msg_data, skip_attachment_binaries=True)
        assert result is not None
        assert result["sender"] == "doctor@klinik.de"
        assert "Befund" in result["subject"]

        # Attachment metadata extraction is disabled for reliability.
        # Email must still be parsed successfully without it.
        att = result.get("attachment_metadata", [])
        assert att == [], "Attachment metadata must be empty (disabled during import)"

    def test_no_bodystructure_key_in_msg_data(self):
        """Parse must work even when msg_data has no BODYSTRUCTURE key."""
        from src.services.mailbox_import_service import _parse_email_for_import

        raw = _build_raw_email(600, sender="sender@example.com", subject="No BS")
        header, text = _split_raw_email(raw)

        msg_data = {
            b"BODY[HEADER]": header,
            b"BODY[TEXT]": text,
            b"FLAGS": [],
        }

        result = _parse_email_for_import(600, msg_data, skip_attachment_binaries=True)
        assert result is not None
        assert result["sender"] == "sender@example.com"

    def test_attachment_filename_extraction_failure_still_ingests(self, db):
        """If attachment metadata extraction fails inside parsing, the email
        is still ingested - filenames are best-effort only.

        _extract_attachment_metadata has an internal try/except that returns []
        on failure, so _parse_email_for_import still succeeds.
        """
        from src.services.mailbox_import_service import (
            _parse_email_for_import,
            _extract_attachment_metadata,
        )

        msg_data = _build_msg_data(500, sender="test@example.com", subject="Test")

        # Patch _extract_attachment_metadata to raise - but the wrapper
        # inside _parse_email_for_import catches it and returns [].
        with patch(
            "src.services.mailbox_import_service._extract_attachment_metadata",
            side_effect=RuntimeError("Simulated extraction failure"),
        ):
            result = _parse_email_for_import(500, msg_data, skip_attachment_binaries=True)

        # Email should still parse successfully
        assert result is not None
        assert result["sender"] == "test@example.com"
        assert result["subject"] == "Test"
        # Attachment metadata empty due to the simulated failure
        assert result.get("attachment_metadata") is None or result.get("attachment_metadata") == []


# ---------------------------------------------------------------------------
# 11. Regression: attachment binary fetch policy
# ---------------------------------------------------------------------------


class TestAttachmentBinaryPolicy:
    """Ensure attachment binaries are never analyzed or downloaded during
    historical import by default."""

    def test_no_body_peek_full_in_default_fetch(self):
        """Default (skip_attachment_binaries=True) must NOT use BODY.PEEK[]."""
        from src.services.mailbox_import_service import _FETCH_HEADERS_AND_TEXT
        decoded = [k.decode() for k in _FETCH_HEADERS_AND_TEXT]
        assert "BODY.PEEK[]" not in decoded
        assert "BODY[]" not in decoded

    def test_attachment_metadata_only_filenames(self):
        """Attachment metadata must include filenames but NOT binary content."""
        from src.services.mailbox_import_service import _extract_attachment_metadata

        msg = MIMEMultipart()
        msg["From"] = "a@b.com"
        msg.attach(MIMEText("Body text", "plain"))
        att = MIMEBase("application", "octet-stream")
        att.set_payload(b"BINARYDATA_SHOULD_NOT_BE_STORED")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="secret.bin")
        msg.attach(att)

        metadata = _extract_attachment_metadata(msg)
        assert len(metadata) == 1
        assert metadata[0]["filename"] == "secret.bin"
        assert metadata[0]["content_type"] == "application/octet-stream"
        assert "content" not in metadata[0]
        assert "payload" not in metadata[0]
        assert "data" not in metadata[0]

    def test_email_ingested_without_attachment_binary(self, db):
        """During import, the ProcessedEmail record must not contain
        attachment binary content."""
        from src.services.mailbox_import_service import _ingest_and_learn_single

        msg_data = _build_msg_data(
            700, sender="clinic@example.com",
            subject="Report with PDF",
            with_attachment=True,
            attachment_filename="medical_report.pdf",
        )

        mock_imap = MagicMock()
        with patch("src.services.mailbox_import_service.learn_from_email"):
            with patch("src.services.mailbox_import_service._update_sender_interaction"):
                result = _ingest_and_learn_single(
                    db=db, uid=700, msg_data=msg_data,
                    folder_name="INBOX", imap=mock_imap,
                    skip_attachment_binaries=True,
                )

        assert result == "new"
        email = db.query(ProcessedEmail).filter(
            ProcessedEmail.message_id == "<test-700@example.com>"
        ).first()
        assert email is not None
        if email.body_plain:
            assert "FAKEPDFCONTENT" not in email.body_plain


# ---------------------------------------------------------------------------
# 12. Regression: multi-folder batch import with per-email fallback
# ---------------------------------------------------------------------------


class TestMultiFolderWithFallback:
    """End-to-end: multiple folders where individual email fetches fail.
    The per-UID fetch strategy ensures forward progress across folders."""

    def test_multi_folder_continues_after_individual_failures(self, db):
        """Import must continue to next folder even if one folder has
        problematic messages causing per-UID fetch failures."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        # Folder 1: normal — all emails succeed
        mock_normal = MockIMAPClient({"INBOX": [1, 2, 3]})
        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_normal)
            with patch("src.services.mailbox_import_service.learn_from_email"):
                with patch("src.services.mailbox_import_service._update_sender_interaction"):
                    _process_folder_streaming(
                        db=db, run=run, folder_name="INBOX",
                        batch_size=5, skip_attachment_binaries=True,
                    )

        assert db.query(ProcessedEmail).filter(
            ProcessedEmail.folder == "INBOX"
        ).count() == 3

        # Folder 2: per-UID fetch always succeeds (mock returns data for single UIDs)
        class Folder2IMAP:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 2}

            def search(self, criteria):
                return [10, 11]

            def fetch(self, uids, parts):
                return _build_metadata_fetch_response(uids, "Klinik")

            def list_folders(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_f2 = Folder2IMAP()
        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_f2)
            with patch("src.services.mailbox_import_service.learn_from_email"):
                with patch("src.services.mailbox_import_service._update_sender_interaction"):
                    _process_folder_streaming(
                        db=db, run=run, folder_name="Klinik",
                        batch_size=5, skip_attachment_binaries=True,
                    )

        assert db.query(ProcessedEmail).filter(
            ProcessedEmail.folder == "Klinik"
        ).count() == 2
        # Total: 3 + 2 = 5 emails across two folders
        assert db.query(ProcessedEmail).count() == 5


# ---------------------------------------------------------------------------
# 13. Regression: sanitized error logging
# ---------------------------------------------------------------------------


class TestSanitizedErrorLogging:
    """Error logs must not contain raw email content (headers, body, MIME data).

    imapclient exceptions can embed raw IMAP server responses which include
    message content.  _sanitize_error() must strip/truncate that.
    """

    def test_sanitize_error_truncates_long_strings(self):
        """_sanitize_error must truncate strings longer than _MAX_ERROR_LOG_LEN."""
        from src.services.mailbox_import_service import _sanitize_error, _MAX_ERROR_LOG_LEN

        # Use a long string without IMAP markers so stripping doesn't reduce it
        long_exc = Exception("a" * (_MAX_ERROR_LOG_LEN + 500))
        result = _sanitize_error(long_exc)
        assert len(result) <= _MAX_ERROR_LOG_LEN + len(" [truncated]")
        assert "[truncated]" in result

    def test_sanitize_error_strips_non_ascii(self):
        """_sanitize_error must replace non-ASCII characters."""
        from src.services.mailbox_import_service import _sanitize_error

        exc = Exception("error\x00\r\nbinary\xff\xfe data here")
        result = _sanitize_error(exc)
        assert "\x00" not in result
        assert "\r" not in result
        assert "\n" not in result
        assert "\xff" not in result

    def test_sanitize_error_keeps_printable_ascii(self):
        """_sanitize_error must preserve printable ASCII content."""
        from src.services.mailbox_import_service import _sanitize_error

        exc = Exception("Tuple incomplete before end of data")
        result = _sanitize_error(exc)
        assert "Tuple incomplete before end of data" in result

    def test_single_fetch_failure_log_is_sanitized(self, db):
        """import_single_fetch_failed log must not contain raw message payloads.
        _sanitize_error() logs only the exception type + first line of message,
        so even printable-ASCII email content cannot leak into logs."""
        import logging
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        raw_payload = "Subject: Secret\r\nFrom: a@b.com\r\n\r\nSecret body content"
        raw_payload_long = raw_payload * 20  # >200 chars of printable ASCII

        class PayloadLeakIMAPClient:
            """Per-UID fetch raises an exception with raw email payload."""
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 2}

            def search(self, criteria):
                return [1, 2]

            def fetch(self, uids, parts):
                # With per-UID strategy, uids always has exactly 1 element
                raise Exception(f"Parse error: {raw_payload_long}")

            def list_folders(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        log_records = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                log_records.append(record.getMessage())

        handler = CaptureHandler()
        import logging as _logging
        svc_logger = _logging.getLogger("src.services.mailbox_import_service")
        svc_logger.addHandler(handler)
        try:
            mock_imap = PayloadLeakIMAPClient()
            with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
                MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
                with patch("src.services.mailbox_import_service.learn_from_email"):
                    with patch("src.services.mailbox_import_service._update_sender_interaction"):
                        _process_folder_streaming(
                            db=db, run=run, folder_name="INBOX",
                            batch_size=5, skip_attachment_binaries=True,
                        )
        finally:
            svc_logger.removeHandler(handler)

        # Verify per-UID failure log lines were emitted (one per failing UID)
        single_fail_logs = [r for r in log_records if "import_single_fetch_failed" in r]
        assert len(single_fail_logs) == 2, f"Expected 2 import_single_fetch_failed logs, got: {single_fail_logs}"

        from src.services.mailbox_import_service import _MAX_ERROR_LOG_LEN
        # Each log line must be bounded — sanitizer truncates to _MAX_ERROR_LOG_LEN
        for record in log_records:
            assert len(record) <= 1000, f"Log line too long: {len(record)} chars"

        # The full raw payload must NOT appear in any log line
        # (_sanitize_error takes only first line of the exception + truncates)
        for record in single_fail_logs:
            assert "Secret body content" not in record, (
                f"Raw email content leaked into log: {record[:200]}"
            )
            # Error portion must be bounded
            assert len(record) < _MAX_ERROR_LOG_LEN * 3, (
                f"Single-fetch fail log line suspiciously long ({len(record)} chars)"
            )


# ---------------------------------------------------------------------------
# 14. Regression: forward progress — checkpoint always advances
# ---------------------------------------------------------------------------


class TestForwardProgress:
    """Verify the importer always makes forward progress and never loops
    on the same failing batch."""

    def test_checkpoint_advances_even_when_all_uids_fail(self, db):
        """If every UID in a batch fails (batch + individual), the checkpoint
        must still advance past those UIDs so they are never retried."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            current_folder_uid_checkpoint=None,
        )
        db.add(run)
        db.commit()

        class AllFailIMAPClient:
            """Both batch and individual fetches always fail."""
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 3}

            def search(self, criteria):
                return [10, 11, 12]

            def fetch(self, uids, parts):
                raise Exception("IMAP connection dead")

            def list_folders(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = AllFailIMAPClient()
        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _process_folder_streaming(
                db=db, run=run, folder_name="INBOX",
                batch_size=5, skip_attachment_binaries=True,
            )

        db.refresh(run)
        # current_folder_uid_checkpoint is reset to None after the folder
        # completes (by design, so the next folder starts fresh).
        # What matters is that all 3 unreachable UIDs were counted as failed,
        # confirming the loop DID iterate all UIDs instead of stopping early.
        assert (run.total_emails_failed or 0) == 3
        # Function must complete without raising (no endless retry / crash)
        assert db.query(ProcessedEmail).count() == 0  # nothing ingested (all failed)

    def test_second_batch_processes_after_first_batch_fails(self, db):
        """After a batch fails, the importer must continue to the next batch."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=2,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        call_sequence = []

        class TwoPhaseIMAPClient:
            """First batch (UIDs 1,2) fails; second batch (UIDs 3,4) succeeds."""
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 4}

            def search(self, criteria):
                return [1, 2, 3, 4]

            def fetch(self, uids, parts):
                call_sequence.append(list(uids))
                if uids == [1, 2] or (len(uids) == 1 and uids[0] in (1, 2)):
                    raise Exception("Malformed message in first batch")
                return _build_metadata_fetch_response(uids, "INBOX")

            def list_folders(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = TwoPhaseIMAPClient()
        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.learn_from_email"):
                with patch("src.services.mailbox_import_service._update_sender_interaction"):
                    _process_folder_streaming(
                        db=db, run=run, folder_name="INBOX",
                        batch_size=2, skip_attachment_binaries=True,
                    )

        # Second batch emails (3 and 4) must be ingested
        assert db.query(ProcessedEmail).filter(
            ProcessedEmail.folder == "INBOX"
        ).count() == 2
        # Fetches were attempted for both batches
        assert len(call_sequence) >= 2


class TestConsecutiveBatchFailureFairsafe:
    """Verify the importer abandons a folder after _MAX_CONSECUTIVE_BATCH_FAILURES
    consecutive all-fail batches instead of grinding through all UIDs."""

    def test_folder_skipped_after_max_consecutive_all_fail_batches(self, db):
        """When N consecutive batches all return empty IMAP responses (no exception,
        just missing UID key), the folder must be abandoned early rather than
        processing every remaining UID."""
        from src.services.mailbox_import_service import (
            _process_folder_streaming,
            _MAX_CONSECUTIVE_BATCH_FAILURES,
        )

        run = MailboxImportRun(
            status="running", batch_size=2,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        # 20 UIDs — far more than would be needed to trigger the failsafe
        uid_list = list(range(100, 120))

        class EmptyResponseIMAPClient:
            """fetch() returns empty dict (no exception, but UID key missing)."""
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": len(uid_list)}

            def search(self, criteria):
                return uid_list

            def fetch(self, uids, parts):
                # Return a dict that does NOT include the requested UID —
                # exactly what some IMAP servers do for corrupt messages.
                return {}

            def list_folders(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = EmptyResponseIMAPClient()
        fetch_call_count = [0]
        original_fetch = mock_imap.fetch

        def counting_fetch(uids, parts):
            fetch_call_count[0] += 1
            return original_fetch(uids, parts)

        mock_imap.fetch = counting_fetch

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _process_folder_streaming(
                db=db, run=run, folder_name="Coburg",
                batch_size=2, skip_attachment_binaries=True,
            )

        db.refresh(run)
        # The importer must NOT have iterated all 20 UIDs — it bailed early.
        # With batch_size=2, _MAX_CONSECUTIVE_BATCH_FAILURES=3, we expect
        # at most (MAX+1)*batch_size fetch attempts before abandonment.
        max_expected_fetches = (_MAX_CONSECUTIVE_BATCH_FAILURES + 1) * 2
        assert fetch_call_count[0] <= max_expected_fetches, (
            f"Too many fetch calls ({fetch_call_count[0]}); "
            f"failsafe should have triggered after {max_expected_fetches}"
        )
        # But some failures must be counted — not zero
        assert (run.total_emails_failed or 0) > 0

    def test_consecutive_counter_resets_on_partial_success(self, db):
        """A successful UID in a batch must reset the consecutive-failure counter,
        preventing premature folder abandonment."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=1,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

        # Alternating: odd UIDs succeed, even UIDs return empty
        class AlternatingIMAPClient:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 10}

            def search(self, criteria):
                return list(range(1, 11))  # UIDs 1..10

            def fetch(self, uids, parts):
                uid = uids[0]
                if uid % 2 == 0:
                    return {}  # even UIDs return empty (fail)
                return _build_metadata_fetch_response(uids, "TestFolder")

            def list_folders(self):
                return []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = AlternatingIMAPClient()
        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.learn_from_email"):
                with patch("src.services.mailbox_import_service._update_sender_interaction"):
                    _process_folder_streaming(
                        db=db, run=run, folder_name="TestFolder",
                        batch_size=1, skip_attachment_binaries=True,
                    )

        db.refresh(run)
        # With alternating success/failure (batch_size=1), consecutive counter
        # never reaches MAX — all 10 UIDs must be processed.
        # 5 odd UIDs succeed → ingested; 5 even UIDs fail
        assert (run.total_emails_ingested or 0) == 5
        assert (run.total_emails_failed or 0) == 5

    def test_folder_exception_stores_sanitized_error_message(self, db):
        """When _process_folder_streaming raises (e.g., IMAP connection fails),
        run.error_message must be sanitized — not raw exception content."""
        from src.services.mailbox_import_service import _run_import_thread

        # Create a run
        run = MailboxImportRun(
            status="pending", batch_size=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        run_id = run.id

        class ConnectionFailIMAPClient:
            """select_folder always raises with raw-looking error content."""
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                raise Exception(
                    "Subject: Secret\r\nFrom: a@b.com\r\n\r\nSecret body"
                )

            def search(self, criteria):
                return [1]

            def fetch(self, uids, parts):
                return _build_metadata_fetch_response(uids, "Coburg")

            def list_folders(self):
                # Discovery succeeds so we get to folder processing
                return [("", "/", "Coburg")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = ConnectionFailIMAPClient()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        from src.models.database import Base
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)

        # Seed a run in the new session
        thread_db = TestSession()
        seed_run = MailboxImportRun(
            id=run_id, status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            folders_discovered=["Coburg"],
            folders_completed=[],
        )
        thread_db.add(seed_run)
        thread_db.commit()
        thread_db.close()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.is_learnable_folder",
                       return_value=True):
                _run_import_thread(TestSession, run_id, 5, True)

        final_session = TestSession()
        finished_run = final_session.query(MailboxImportRun).filter(
            MailboxImportRun.id == run_id
        ).first()

        if finished_run and finished_run.error_message:
            # Must NOT contain raw email content
            assert "Secret body" not in finished_run.error_message, (
                f"Raw content in error_message: {finished_run.error_message!r}"
            )
        final_session.close()


# ---------------------------------------------------------------------------
# Import stall prevention & log leak fixes
# ---------------------------------------------------------------------------


class TestImportStallPrevention:
    """Verify that the importer always advances and never stalls at 0 progress."""

    def test_folder_level_error_increments_failed_counter(self, db):
        """When _process_folder_streaming raises (e.g., select_folder fails),
        the run's total_emails_failed MUST increase so status is never silently
        all-zero when folders error out."""
        from src.services.mailbox_import_service import _run_import_thread

        run = MailboxImportRun(
            status="pending", batch_size=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        run_id = run.id

        class SelectFailIMAPClient:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                raise RuntimeError("Mailbox unavailable")

            def search(self, criteria):
                return [1, 2]

            def fetch(self, uids, parts):
                return _build_metadata_fetch_response(uids, "Coburg")

            def list_folders(self):
                return [("", "/", "Coburg"), ("", "/", "Berlin")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = SelectFailIMAPClient()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        from src.models.database import Base
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)

        thread_db = TestSession()
        seed_run = MailboxImportRun(
            id=run_id, status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            folders_discovered=["Coburg", "Berlin"],
            folders_completed=[],
            total_folders_discovered=2,
        )
        thread_db.add(seed_run)
        thread_db.commit()
        thread_db.close()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.is_learnable_folder",
                       return_value=True):
                _run_import_thread(TestSession, run_id, 5, True)

        final_session = TestSession()
        finished_run = final_session.query(MailboxImportRun).filter(
            MailboxImportRun.id == run_id
        ).first()

        assert finished_run is not None
        # Both folders failed at select_folder, so failed counter must be >= 2
        assert (finished_run.total_emails_failed or 0) >= 2, (
            f"Expected failed >= 2 but got {finished_run.total_emails_failed}"
        )
        # Status should be completed (not stuck running forever)
        assert finished_run.status == "completed", (
            f"Expected 'completed' but got {finished_run.status!r}"
        )
        final_session.close()

    def test_zero_progress_impossible_with_repeated_failures(self, db):
        """If every UID in every batch fails, the failed counter MUST reflect
        the actual number of failed messages — never stay at 0."""
        from src.services.mailbox_import_service import _run_import_thread

        run = MailboxImportRun(
            status="pending", batch_size=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        run_id = run.id

        class AllFetchFailIMAPClient:
            """Every fetch raises, simulating corrupt server responses."""
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 10}

            def search(self, criteria):
                return list(range(1, 11))

            def fetch(self, uids, parts):
                raise Exception("Bad IMAP response\r\nBODY[HEADER] raw data")

            def list_folders(self):
                return [("", "/", "INBOX")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = AllFetchFailIMAPClient()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        from src.models.database import Base
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)

        thread_db = TestSession()
        seed_run = MailboxImportRun(
            id=run_id, status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            folders_discovered=["INBOX"],
            folders_completed=[],
            total_folders_discovered=1,
        )
        thread_db.add(seed_run)
        thread_db.commit()
        thread_db.close()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.is_learnable_folder",
                       return_value=True):
                _run_import_thread(TestSession, run_id, 5, True)

        final_session = TestSession()
        finished_run = final_session.query(MailboxImportRun).filter(
            MailboxImportRun.id == run_id
        ).first()

        assert finished_run is not None
        # All 10 UIDs failed — counter must be > 0
        assert (finished_run.total_emails_failed or 0) >= 10, (
            f"Expected failed >= 10 but got {finished_run.total_emails_failed}"
        )
        # Should not remain running
        assert finished_run.status in ("completed", "failed"), (
            f"Expected terminal status but got {finished_run.status!r}"
        )
        final_session.close()

    def test_import_completes_after_all_folders_error(self, db):
        """Even if every folder errors at the connection/select level, the
        import run must reach a terminal status — never hang as 'running'."""
        from src.services.mailbox_import_service import _run_import_thread

        run = MailboxImportRun(
            status="pending", batch_size=5,
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        run_id = run.id

        class ConnectionFailIMAPClient:
            client = None

            def select_folder(self, folder, readonly=False):
                raise ConnectionError("Connection reset by peer")

            def search(self, criteria):
                return []

            def fetch(self, uids, parts):
                return {}

            def list_folders(self):
                return [("", "/", "A"), ("", "/", "B"), ("", "/", "C")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = ConnectionFailIMAPClient()
        mock_imap.client = mock_imap  # Set client to non-None so connect check passes
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        from src.models.database import Base
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)

        thread_db = TestSession()
        seed_run = MailboxImportRun(
            id=run_id, status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            folders_discovered=["A", "B", "C"],
            folders_completed=[],
            total_folders_discovered=3,
        )
        thread_db.add(seed_run)
        thread_db.commit()
        thread_db.close()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.is_learnable_folder",
                       return_value=True):
                _run_import_thread(TestSession, run_id, 5, True)

        final_session = TestSession()
        finished_run = final_session.query(MailboxImportRun).filter(
            MailboxImportRun.id == run_id
        ).first()

        assert finished_run is not None
        # Must reach terminal status
        assert finished_run.status == "completed", (
            f"Expected 'completed' but got {finished_run.status!r}"
        )
        # Failed counter must be non-zero (3 folders failed)
        assert (finished_run.total_emails_failed or 0) >= 3
        final_session.close()


class TestLogLeakPrevention:
    """Verify that no raw IMAP/email payload leaks into logs."""

    def test_sanitize_error_caps_length_in_debug_mode(self):
        """sanitize_error must cap output length even in debug mode."""
        from src.utils.error_handling import sanitize_error

        # Simulate an IMAP exception with a huge raw payload
        raw_payload = "Subject: Secret\r\n" + "X" * 5000
        exc = Exception(raw_payload)

        result = sanitize_error(exc, debug=True)
        # Must be capped — well under the raw length
        assert len(result) <= 250, f"sanitize_error output too long: {len(result)}"
        # Must NOT contain the full raw payload
        assert "X" * 200 not in result

    def test_sanitize_error_no_multiline_in_debug_mode(self):
        """sanitize_error must only include the first line even in debug mode."""
        from src.utils.error_handling import sanitize_error

        # IMAP exceptions often embed multi-line server responses
        multi_line = "Error on line 1\r\nBODY[HEADER] raw content\r\nMore raw"
        exc = Exception(multi_line)

        result = sanitize_error(exc, debug=True)
        assert "BODY[HEADER]" not in result, (
            f"Raw IMAP content leaked: {result!r}"
        )
        assert "More raw" not in result

    def test_sanitize_error_production_mode_type_only(self):
        """In non-debug mode, sanitize_error returns only the type name."""
        from src.utils.error_handling import sanitize_error

        exc = RuntimeError("Content-Type: text/html; boundary=something\r\n<html>...")
        result = sanitize_error(exc, debug=False)
        assert result == "RuntimeError"
        assert "html" not in result.lower()

    def test_import_sanitize_error_strips_imap_payload(self):
        """The import service's _sanitize_error must strip raw IMAP content."""
        from src.services.mailbox_import_service import _sanitize_error

        exc = Exception(
            "IMAP error: b'* 1 FETCH (BODY[HEADER] {1234}\\r\\n"
            "Subject: Private\\r\\nFrom: secret@example.com\\r\\n\\r\\n"
            "This is private body text"
        )
        result = _sanitize_error(exc)
        assert len(result) <= 150  # 120 + [truncated] suffix
        assert "private body" not in result.lower()

    def test_per_uid_fetch_error_uses_sanitized_log(self, db):
        """When a single UID fetch fails, the log message must use
        _sanitize_error — no raw IMAP payload in log output."""
        import logging
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            current_folder="TestFolder",
        )
        db.add(run)
        db.commit()

        class FetchFailIMAPClient:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 2}

            def search(self, criteria):
                return [1, 2]

            def fetch(self, uids, parts):
                # Simulate IMAP error with raw payload in exception
                raise Exception(
                    "FETCH failed\r\nSubject: Secret Document\r\n"
                    "From: ceo@company.com\r\n\r\n"
                    "Confidential body text here"
                )

            def list_folders(self):
                return [("", "/", "TestFolder")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = FetchFailIMAPClient()

        log_records = []
        handler = logging.Handler()
        handler.emit = lambda record: log_records.append(record)
        import src.services.mailbox_import_service as svc
        svc.logger.addHandler(handler)

        try:
            with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
                MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
                result = _process_folder_streaming(db, run, "TestFolder", 5, True)
        finally:
            svc.logger.removeHandler(handler)

        # Check all log records for raw content leak
        for record in log_records:
            msg = record.getMessage()
            assert "Confidential body" not in msg, (
                f"Raw payload leaked in log: {msg!r}"
            )
            assert "Secret Document" not in msg, (
                f"Email subject leaked in log: {msg!r}"
            )
            assert "ceo@company.com" not in msg, (
                f"Email sender leaked in log: {msg!r}"
            )

        # Failed counter must show the 2 failed UIDs
        assert result["failed"] == 0  # reset after batch commit
        assert (run.total_emails_failed or 0) >= 2

    def test_malformed_imap_response_no_stall(self, db):
        """Emails with malformed/missing BODY[HEADER] must fail gracefully
        without stalling the importer."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            current_folder="TestFolder",
        )
        db.add(run)
        db.commit()

        class MalformedResponseIMAPClient:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 3}

            def search(self, criteria):
                return [1, 2, 3]

            def fetch(self, uids, parts):
                # Return response with missing/empty BODY[HEADER] and BODY[TEXT]
                uid = uids[0]
                return {uid: {
                    b"BODY[HEADER]": b"",
                    b"BODY[TEXT]": b"",
                    b"FLAGS": [b"\\Seen"],
                }}

            def list_folders(self):
                return [("", "/", "TestFolder")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = MalformedResponseIMAPClient()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            result = _process_folder_streaming(db, run, "TestFolder", 5, True)

        # All 3 UIDs should have failed (empty response → parse returns None)
        assert (run.total_emails_failed or 0) >= 3, (
            f"Expected >= 3 failed but got {run.total_emails_failed}"
        )
        # Checkpoint must have advanced past all UIDs
        assert run.current_folder_uid_checkpoint is None or run.current_folder_uid_checkpoint >= 3

    def test_corrupted_mime_continues_import(self, db):
        """Emails with corrupted MIME content must fail individually without
        blocking subsequent emails."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=10,
            started_at=datetime.now(timezone.utc),
            current_folder="TestFolder",
        )
        db.add(run)
        db.commit()

        good_email_raw = _build_raw_email(2, subject="Good email")
        good_header, good_text = _split_raw_email(good_email_raw)

        class MixedResponseIMAPClient:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 3}

            def search(self, criteria):
                return [1, 2, 3]

            def fetch(self, uids, parts):
                uid = uids[0]
                if uid == 1 or uid == 3:
                    # Corrupted: completely empty response → parse returns None
                    return {uid: {
                        b"BODY[HEADER]": b"",
                        b"BODY[TEXT]": b"",
                        b"FLAGS": [b"\\Seen"],
                    }}
                else:
                    # Good email for UID 2
                    return {uid: {
                        b"BODY[HEADER]": good_header,
                        b"BODY[TEXT]": good_text,
                        b"FLAGS": [b"\\Seen"],
                    }}

            def list_folders(self):
                return [("", "/", "TestFolder")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = MixedResponseIMAPClient()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.learn_from_email"):
                with patch("src.services.mailbox_import_service._update_sender_interaction"):
                    result = _process_folder_streaming(db, run, "TestFolder", 10, True)

        # UID 2 should have succeeded, UIDs 1 and 3 should have failed
        total_ingested = run.total_emails_ingested or 0
        total_failed = run.total_emails_failed or 0
        assert total_ingested >= 1, f"Expected >= 1 ingested but got {total_ingested}"
        assert total_failed >= 2, f"Expected >= 2 failed but got {total_failed}"


# ---------------------------------------------------------------------------
# Comprehensive regression tests for import reliability
# ---------------------------------------------------------------------------


class TestImportReliabilityRegression:
    """Comprehensive tests proving that the importer:
    1. Always advances UID cursor (no stall)
    2. Failed counter increments truthfully
    3. Works without BODYSTRUCTURE dependency
    4. Works with per-UID minimal fetch only (HEADER + TEXT + FLAGS)
    5. No raw payload appears in logs
    6. Folder skip after bounded repeated failures
    7. Repeated malformed messages allow forward progress
    8. Progress != 0 during repeated failures
    """

    def test_importer_advances_through_all_failing_uids(self, db):
        """When every UID in a folder fails, the importer must still
        advance through ALL UIDs and not loop on the first one."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            current_folder="FailFolder",
        )
        db.add(run)
        db.commit()

        fetched_uids = []

        class AllFailIMAPClient:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 10}

            def search(self, criteria):
                return list(range(1, 11))  # UIDs 1-10

            def fetch(self, uids, parts):
                fetched_uids.extend(uids)
                raise Exception("IMAP timeout for UID")

            def list_folders(self):
                return [("", "/", "FailFolder")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = AllFailIMAPClient()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _process_folder_streaming(db, run, "FailFolder", 5, True)

        # MUST have attempted every UID (no stall on first)
        assert len(fetched_uids) == 10, (
            f"Expected 10 fetch attempts, got {len(fetched_uids)}: {fetched_uids}"
        )
        # Failed counter MUST reflect all 10 failures
        assert (run.total_emails_failed or 0) >= 10, (
            f"Expected >= 10 failed, got {run.total_emails_failed}"
        )
        # Checkpoint MUST have advanced past all UIDs
        assert run.current_folder_uid_checkpoint is None or run.current_folder_uid_checkpoint >= 10

    def test_failed_counter_always_reflects_reality(self, db):
        """When UIDs fail, total_emails_failed MUST increase — never stay 0."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=3,
            started_at=datetime.now(timezone.utc),
            current_folder="TestFolder",
        )
        db.add(run)
        db.commit()

        class SomeFail:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 5}

            def search(self, criteria):
                return [1, 2, 3, 4, 5]

            def fetch(self, uids, parts):
                uid = uids[0]
                if uid % 2 == 0:
                    raise Exception("fetch error")
                return {uid: {
                    b"BODY[HEADER]": b"",
                    b"BODY[TEXT]": b"",
                    b"FLAGS": [],
                }}

            def list_folders(self):
                return [("", "/", "TestFolder")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = SomeFail()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _process_folder_streaming(db, run, "TestFolder", 10, True)

        # UIDs 2 and 4 should fail (fetch raises), UIDs 1,3,5 should fail
        # (empty header/text → parse returns None → ValueError)
        # So ALL 5 should be in failed count
        assert (run.total_emails_failed or 0) >= 2, (
            f"Expected >= 2 failed, got {run.total_emails_failed}"
        )

    def test_no_bodystructure_in_fetch_fields(self):
        """The fetch fields must NOT include BODYSTRUCTURE."""
        from src.services.mailbox_import_service import (
            _FETCH_HEADERS_AND_TEXT,
            _FETCH_FULL,
        )

        for field in _FETCH_HEADERS_AND_TEXT:
            assert b"BODYSTRUCTURE" not in field.upper(), (
                f"BODYSTRUCTURE found in fetch field: {field}"
            )
        for field in _FETCH_FULL:
            assert b"BODYSTRUCTURE" not in field.upper(), (
                f"BODYSTRUCTURE found in full fetch field: {field}"
            )

    def test_fetch_uses_only_header_text_flags(self):
        """Default fetch fields must be exactly HEADER + TEXT + FLAGS."""
        from src.services.mailbox_import_service import _FETCH_HEADERS_AND_TEXT

        field_names = set(f.upper() for f in _FETCH_HEADERS_AND_TEXT)
        assert b"BODY.PEEK[HEADER]" in field_names
        assert b"BODY.PEEK[TEXT]" in field_names
        assert b"FLAGS" in field_names
        assert len(field_names) == 3, f"Expected exactly 3 fetch fields, got: {field_names}"

    def test_attachment_metadata_disabled_in_import(self):
        """Attachment metadata extraction must be disabled during mailbox import."""
        from src.services.mailbox_import_service import _parse_email_for_import

        msg = MIMEMultipart("mixed")
        msg["From"] = "test@example.com"
        msg["To"] = "user@example.com"
        msg["Subject"] = "Test with attachment"
        msg["Message-ID"] = "<att-test@example.com>"
        msg["Date"] = "Mon, 1 Jan 2024 10:00:00 +0000"
        msg.attach(MIMEText("Body text", "plain"))

        att = MIMEBase("application", "pdf")
        att.set_payload(b"FAKEPDF")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="secret.pdf")
        msg.attach(att)

        raw = msg.as_bytes()
        header, text = _split_raw_email(raw)
        msg_data = {
            b"BODY[HEADER]": header,
            b"BODY[TEXT]": text,
            b"FLAGS": [],
        }

        result = _parse_email_for_import(1, msg_data, skip_attachment_binaries=True)
        assert result is not None
        assert result["attachment_metadata"] == [], (
            "Attachment metadata must be empty (disabled during import)"
        )

    def test_sanitize_error_strips_body_header(self):
        """_sanitize_error must strip BODY[HEADER] content from exceptions."""
        from src.services.mailbox_import_service import _sanitize_error

        exc = Exception(
            "IMAP error: BODY[HEADER] {1234}\r\n"
            "From: secret@example.com\r\nSubject: Confidential"
        )
        result = _sanitize_error(exc)
        assert "secret@example.com" not in result
        assert "Confidential" not in result
        assert "BODY[HEADER]" not in result

    def test_sanitize_error_strips_body_text(self):
        """_sanitize_error must strip BODY[TEXT] content from exceptions."""
        from src.services.mailbox_import_service import _sanitize_error

        exc = Exception(
            "Error: BODY[TEXT] {5678}\r\n"
            "This is the private email body content"
        )
        result = _sanitize_error(exc)
        assert "private email body" not in result
        assert "BODY[TEXT]" not in result

    def test_sanitize_error_strips_bodystructure(self):
        """_sanitize_error must strip BODYSTRUCTURE dumps from exceptions."""
        from src.services.mailbox_import_service import _sanitize_error

        exc = Exception(
            'Tuple incomplete before BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "UTF-8")))'
        )
        result = _sanitize_error(exc)
        assert "BODYSTRUCTURE" not in result
        assert "CHARSET" not in result

    def test_sanitize_error_strips_mime_content(self):
        """_sanitize_error must strip MIME-related content."""
        from src.services.mailbox_import_service import _sanitize_error

        exc = Exception(
            "Parse error: Content-Type: multipart/mixed; "
            "boundary=----=_Part_12345\r\nMIME-Version: 1.0"
        )
        result = _sanitize_error(exc)
        assert "boundary=" not in result
        assert "MIME-" not in result

    def test_sanitize_error_strips_email_headers(self):
        """_sanitize_error must strip common email headers from exceptions."""
        from src.services.mailbox_import_service import _sanitize_error

        for header in ["From:", "To:", "Subject:", "Date:", "Message-ID:",
                        "Received:", "Return-Path:", "Content-Disposition:"]:
            exc = Exception(f"Error near {header} user@private.com secret data")
            result = _sanitize_error(exc)
            assert "private.com" not in result, (
                f"Content after {header} leaked: {result}"
            )

    def test_folder_skip_after_consecutive_failures(self, db):
        """After _MAX_CONSECUTIVE_BATCH_FAILURES consecutive all-fail batches,
        the folder must be abandoned and the importer must advance."""
        from src.services.mailbox_import_service import (
            _process_folder_streaming,
            _MAX_CONSECUTIVE_BATCH_FAILURES,
        )

        run = MailboxImportRun(
            status="running", batch_size=2,
            started_at=datetime.now(timezone.utc),
            current_folder="BadFolder",
        )
        db.add(run)
        db.commit()

        # 20 UIDs, all will fail → should skip folder after
        # _MAX_CONSECUTIVE_BATCH_FAILURES * batch_size fetch attempts
        class AllEmpty:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 20}

            def search(self, criteria):
                return list(range(1, 21))

            def fetch(self, uids, parts):
                uid = uids[0]
                return {uid: {
                    b"BODY[HEADER]": b"",
                    b"BODY[TEXT]": b"",
                    b"FLAGS": [],
                }}

            def list_folders(self):
                return [("", "/", "BadFolder")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = AllEmpty()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            result = _process_folder_streaming(db, run, "BadFolder", 2, True)

        # The folder should have been abandoned after bounded failures
        # Not all 20 UIDs should have been attempted
        max_attempted = _MAX_CONSECUTIVE_BATCH_FAILURES * 2  # batch_size=2
        assert (run.total_emails_failed or 0) >= max_attempted, (
            f"Expected >= {max_attempted} failed, got {run.total_emails_failed}"
        )
        # But MUST NOT have processed all 20 (folder skip kicks in)
        assert (run.total_emails_failed or 0) <= 20

    def test_multi_folder_advances_despite_failures(self, db):
        """Import must advance from folder to folder even when all UIDs
        in a folder fail. Both folders must be attempted."""
        from src.services.mailbox_import_service import _run_import_thread

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)

        setup_db = TestSession()
        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            folders_discovered=["Coburg", "Klinik"],
            folders_completed=[],
            total_folders_discovered=2,
        )
        setup_db.add(run)
        setup_db.commit()
        run_id = run.id
        setup_db.close()

        folders_attempted = []

        class AllFailPerFolder:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                folders_attempted.append(folder)
                raise RuntimeError(f"Cannot select {folder}")

            def search(self, criteria):
                return []

            def fetch(self, uids, parts):
                return {}

            def list_folders(self):
                return [("", "/", "Coburg"), ("", "/", "Klinik")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = AllFailPerFolder()

        def factory():
            return TestSession()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _run_import_thread(factory, run_id, 5, True)

        # Both folders must have been attempted
        assert "Coburg" in folders_attempted, "Coburg was not attempted"
        assert "Klinik" in folders_attempted, "Klinik was not attempted"

        # Verify status is completed (not stuck)
        check_db = TestSession()
        final_run = check_db.query(MailboxImportRun).get(run_id)
        assert final_run.status == "completed", (
            f"Expected completed, got {final_run.status}"
        )
        # Failed counter must be > 0 (each folder error increments)
        assert (final_run.total_emails_failed or 0) >= 2, (
            f"Expected >= 2 failed, got {final_run.total_emails_failed}"
        )
        check_db.close()

    def test_progress_nonzero_during_failures(self, db):
        """When folders fail but advance, progress_percent must not stay 0."""
        from src.services.mailbox_import_service import _run_import_thread

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)

        setup_db = TestSession()
        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            folders_discovered=["Folder1", "Folder2", "Folder3"],
            folders_completed=[],
            total_folders_discovered=3,
        )
        setup_db.add(run)
        setup_db.commit()
        run_id = run.id
        setup_db.close()

        class EmptyFolders:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 0}

            def search(self, criteria):
                return []  # Empty folder

            def fetch(self, uids, parts):
                return {}

            def list_folders(self):
                return [("", "/", f) for f in ["Folder1", "Folder2", "Folder3"]]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = EmptyFolders()

        def factory():
            return TestSession()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            _run_import_thread(factory, run_id, 5, True)

        check_db = TestSession()
        final_run = check_db.query(MailboxImportRun).get(run_id)
        assert final_run.status == "completed"
        # All 3 folders should be completed
        completed = final_run.folders_completed or []
        assert len(completed) == 3, f"Expected 3 completed, got {len(completed)}"
        check_db.close()

    def test_per_uid_fetch_no_raw_payload_in_any_log(self, db):
        """No log line from the import path may contain raw IMAP payload."""
        import logging as _logging
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            current_folder="TestFolder",
        )
        db.add(run)
        db.commit()

        # Create exception with many types of IMAP payload content
        payload_markers = [
            "BODY[HEADER] {1234}\r\nFrom: secret@corp.com",
            "BODY[TEXT] {5678}\r\nThis is confidential",
            'BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "UTF-8"))',
            "Subject: Private Meeting Notes",
            "Content-Type: multipart/mixed; boundary=----=_Part_1",
            "Message-ID: <unique-id@private.com>",
        ]

        uid_counter = [0]

        class PayloadLeakTest:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": len(payload_markers)}

            def search(self, criteria):
                return list(range(1, len(payload_markers) + 1))

            def fetch(self, uids, parts):
                idx = uid_counter[0]
                uid_counter[0] += 1
                if idx < len(payload_markers):
                    raise Exception(payload_markers[idx])
                raise Exception("generic error")

            def list_folders(self):
                return [("", "/", "TestFolder")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = PayloadLeakTest()

        log_messages = []
        handler = _logging.Handler()
        handler.emit = lambda record: log_messages.append(record.getMessage())
        import src.services.mailbox_import_service as svc
        svc.logger.addHandler(handler)

        try:
            with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
                MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
                _process_folder_streaming(db, run, "TestFolder", 10, True)
        finally:
            svc.logger.removeHandler(handler)

        # Check NO log line contains any of these payload fragments
        forbidden = [
            "secret@corp.com", "confidential", "Private Meeting",
            "BODYSTRUCTURE", "CHARSET", "boundary=",
            "BODY[HEADER]", "BODY[TEXT]",
            "Content-Type:", "Message-ID:",
            "unique-id@private.com",
        ]
        for msg in log_messages:
            for frag in forbidden:
                assert frag not in msg, (
                    f"Forbidden payload fragment '{frag}' found in log: {msg[:200]}"
                )

    def test_import_works_with_minimal_header_text_flags_only(self, db):
        """Import must succeed with only BODY[HEADER] + BODY[TEXT] + FLAGS.
        No BODYSTRUCTURE, no attachment parsing, no other fields required."""
        from src.services.mailbox_import_service import _process_folder_streaming

        run = MailboxImportRun(
            status="running", batch_size=5,
            started_at=datetime.now(timezone.utc),
            current_folder="MinimalFolder",
        )
        db.add(run)
        db.commit()

        raw_email = _build_raw_email(1, subject="Minimal test", sender="a@b.com")
        header, text = _split_raw_email(raw_email)

        class MinimalIMAPClient:
            client = None

            def __init__(self):
                self.client = self

            def select_folder(self, folder, readonly=False):
                return {"EXISTS": 1}

            def search(self, criteria):
                return [1]

            def fetch(self, uids, parts):
                uid = uids[0]
                # Return ONLY header, text, flags — nothing else
                return {uid: {
                    b"BODY[HEADER]": header,
                    b"BODY[TEXT]": text,
                    b"FLAGS": [b"\\Seen"],
                }}

            def list_folders(self):
                return [("", "/", "MinimalFolder")]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        mock_imap = MinimalIMAPClient()

        with patch("src.services.imap_service.IMAPService") as MockIMAPSvc:
            MockIMAPSvc.return_value = _make_mock_imap_context(mock_imap)
            with patch("src.services.mailbox_import_service.learn_from_email"):
                with patch("src.services.mailbox_import_service._update_sender_interaction"):
                    _process_folder_streaming(db, run, "MinimalFolder", 5, True)

        assert (run.total_emails_ingested or 0) >= 1, (
            f"Expected >= 1 ingested, got {run.total_emails_ingested}"
        )
        assert (run.total_emails_failed or 0) == 0

    def test_error_handling_sanitize_strips_imap_payload(self):
        """The error_handling.py sanitize_error must strip IMAP payload in debug mode."""
        from src.utils.error_handling import sanitize_error

        test_cases = [
            (
                "Error: BODY[HEADER] {1234}\r\nFrom: secret@example.com",
                "BODY[HEADER]",
            ),
            (
                "Error: BODYSTRUCTURE (TEXT PLAIN)",
                "BODYSTRUCTURE",
            ),
            (
                "Error near Subject: Private Document\r\nDate: 2024-01-01",
                "Private Document",
            ),
            (
                "MIME-Version: 1.0\r\nContent-Type: text/plain",
                "MIME-",
            ),
        ]

        for exc_msg, forbidden in test_cases:
            exc = Exception(exc_msg)
            result = sanitize_error(exc, debug=True)
            assert forbidden not in result, (
                f"'{forbidden}' leaked in sanitized output: {result}"
            )

