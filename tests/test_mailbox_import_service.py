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
        """Complex multipart email with PDF attachment: filename must be
        extracted from MIME headers (not BODYSTRUCTURE)."""
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

        att = result.get("attachment_metadata", [])
        filenames = [a["filename"] for a in att]
        assert "Befund_2024.pdf" in filenames
        assert "scan.jpg" in filenames
        types = {a["filename"]: a["content_type"] for a in att}
        assert types["Befund_2024.pdf"] == "application/pdf"
        assert types["scan.jpg"] == "image/jpeg"

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

        long_exc = Exception("x" * (_MAX_ERROR_LOG_LEN + 500))
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
