from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models.database import Base, ProcessedEmail, ActionQueue
from src.services.thread_context import (
    infer_thread_state,
    update_thread_state_for_thread,
)


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_infer_thread_state_rule_order():
    assert (
        infer_thread_state(
            has_action_required=True,
            last_sender_is_user=True,
            has_resolved=True,
            open_actions_count=0,
        )
        == "waiting_for_me"
    )
    assert (
        infer_thread_state(
            has_action_required=False,
            last_sender_is_user=True,
            has_resolved=False,
            open_actions_count=3,
        )
        == "waiting_for_other"
    )
    assert (
        infer_thread_state(
            has_action_required=False,
            last_sender_is_user=False,
            has_resolved=True,
            open_actions_count=4,
        )
        == "resolved"
    )
    assert (
        infer_thread_state(
            has_action_required=False,
            last_sender_is_user=False,
            has_resolved=False,
            open_actions_count=2,
        )
        == "informational"
    )


def test_update_thread_state_for_thread_handles_missing_and_partial_data():
    db = _session()
    try:
        assert (
            update_thread_state_for_thread(
                db, thread_id=None, user_address="me@example.com"
            )
            == "informational"
        )

        email = ProcessedEmail(
            message_id="thread-test@example.com",
            uid="1001",
            thread_id="thread-t1",
            sender="me@example.com",
            action_required=False,
            is_resolved=False,
            is_processed=True,
            processed_at=datetime.utcnow(),
        )
        db.add(email)
        db.commit()

        state = update_thread_state_for_thread(
            db, thread_id="thread-t1", user_address="me@example.com"
        )
        db.refresh(email)
        assert state == "waiting_for_other"
        assert email.thread_state == "waiting_for_other"

        db.add(
            ActionQueue(
                email_id=email.id,
                thread_id="thread-t1",
                action_type="mark_read",
                payload={},
                status="approved",
            )
        )
        db.commit()
        state_with_open_actions = update_thread_state_for_thread(
            db, thread_id="thread-t1", user_address="other@example.com"
        )
        db.refresh(email)
        assert state_with_open_actions == "informational"
        assert email.thread_state == "informational"
    finally:
        db.close()
