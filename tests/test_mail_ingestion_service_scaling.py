import logging
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, ProcessedEmail
from src.services.mail_ingestion_service import MailIngestionService


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _make_message_data(message_id: str) -> bytes:
    return (
        f"From: sender@example.com\r\n"
        f"Subject: Test\r\n"
        f"Message-ID: {message_id}\r\n\r\n"
        "Body"
    ).encode("utf-8")


def _make_imap_context(message_uids, message_id_map=None):
    client = MagicMock()
    client.search.return_value = message_uids

    def _fetch(batch, _fields):
        data = {}
        for uid in batch:
            message_id = (
                message_id_map.get(uid, f"<msg-{uid}@test.com>")
                if message_id_map
                else f"<msg-{uid}@test.com>"
            )
            data[uid] = {b"BODY[]": _make_message_data(message_id), b"FLAGS": []}
        return data

    client.fetch.side_effect = _fetch

    imap_instance = MagicMock()
    imap_instance.client = client

    def _parse_email(uid, _message_data):
        return {
            "uid": str(uid),
            "message_id": (
                message_id_map.get(uid, f"<msg-{uid}@test.com>")
                if message_id_map
                else f"<msg-{uid}@test.com>"
            ),
            "in_reply_to": "",
            "references": "",
            "subject": "Test",
            "sender": "sender@example.com",
            "recipients": "recipient@example.com",
            "date": None,
            "body_plain": "Body",
            "body_html": "",
            "integrity_hash": f"hash-{uid}",
        }

    imap_instance._parse_email.side_effect = _parse_email

    imap_context = MagicMock()
    imap_context.__enter__.return_value = imap_instance
    imap_context.__exit__.return_value = False
    return imap_context, client


def test_ingestion_scans_more_than_200_messages_without_cap():
    db = _make_db_session()
    service = MailIngestionService(db)
    message_uids = list(range(1, 251))
    imap_context, client = _make_imap_context(message_uids)

    with patch("src.services.mail_ingestion_service.IMAPService", return_value=imap_context):
        stats = service.ingest_folder(folder="INBOX", run_id="run-over-200")

    assert stats["total"] == 250
    assert stats["new"] == 250
    assert stats["skipped"] == 0
    assert client.fetch.call_count == 10
    fetched_uids = [uid for call in client.fetch.call_args_list for uid in call.args[0]]
    assert fetched_uids == message_uids


def test_ingestion_uses_uid_checkpoint_to_process_only_new_messages(caplog):
    db = _make_db_session()
    for uid in range(1, 251):
        db.add(
            ProcessedEmail(
                message_id=f"<existing-{uid}@test.com>",
                uid=str(uid),
                imap_uid=str(uid),
                folder="INBOX",
            )
        )
    db.commit()

    service = MailIngestionService(db)
    message_uids = list(range(1, 261))
    imap_context, _client = _make_imap_context(message_uids)

    with caplog.at_level(logging.INFO):
        with patch("src.services.mail_ingestion_service.IMAPService", return_value=imap_context):
            stats = service.ingest_folder(folder="INBOX", run_id="run-uid-checkpoint")

    assert stats["total"] == 10
    assert stats["new"] == 10
    assert stats["skipped"] == 0

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "server_total=260" in logs
    assert "uid_checkpoint=250" in logs
    assert "considered=10" in logs


def test_ingestion_deduplication_still_skips_known_message_ids():
    db = _make_db_session()
    db.add(
        ProcessedEmail(
            message_id="<dup@test.com>",
            uid="legacy",
            imap_uid=None,
            folder="INBOX",
        )
    )
    db.commit()

    service = MailIngestionService(db)
    message_uids = [1, 2]
    message_id_map = {
        1: "<dup@test.com>",
        2: "<new@test.com>",
    }
    imap_context, _client = _make_imap_context(message_uids, message_id_map=message_id_map)

    with patch("src.services.mail_ingestion_service.IMAPService", return_value=imap_context):
        stats = service.ingest_folder(folder="INBOX", run_id="run-dedup")

    assert stats["total"] == 2
    assert stats["new"] == 1
    assert stats["skipped"] == 1
    assert db.query(ProcessedEmail).count() == 2
