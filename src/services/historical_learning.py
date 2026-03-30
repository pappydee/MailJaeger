"""
Historical Learning Service for MailJaeger.

Builds learning aggregates from historical mailbox data:
  - SenderProfile: per-sender/domain behavior stats
  - FolderPlacementAggregate: sender/keyword/category -> folder patterns
  - ReplyPattern: reply probability and delay per sender/category

All aggregates are:
  - Deterministic (simple counts and statistics)
  - Incrementally updateable
  - Explainable (no black-box models)
  - Queryable for prediction
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.database import (
    ProcessedEmail,
    SenderProfile,
    FolderPlacementAggregate,
    ReplyPattern,
    UserActionEvent,
)
from src.services.folder_classifier import (
    classify_folder,
    extract_sender_domain,
    extract_sender_address,
    extract_subject_keywords,
    FOLDER_TYPE_SENT,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


def learn_from_email(db: Session, email: ProcessedEmail, source: str = "imported-history") -> Dict[str, Any]:
    """Learn from a single email's current folder placement.

    Updates:
      - SenderProfile (domain-level)
      - FolderPlacementAggregate (sender_domain, sender_address, subject keywords, category)

    Args:
        db: Database session
        email: The email to learn from
        source: Source of the signal (imported-history, user, system)

    Returns:
        Dict with counts of aggregates updated.
    """
    stats = {"sender_profiles": 0, "folder_aggregates": 0}
    if not email or not email.folder:
        return stats

    sender = email.sender or ""
    domain = extract_sender_domain(sender)
    address = extract_sender_address(sender)
    folder = email.folder
    category = email.category or ""

    # Update sender profile (domain-level)
    if domain:
        _update_sender_profile_for_email(db, domain, folder, email)
        stats["sender_profiles"] += 1

    # Update folder placement aggregates
    if domain:
        _upsert_folder_aggregate(db, "sender_domain", domain, folder)
        stats["folder_aggregates"] += 1

    if address:
        _upsert_folder_aggregate(db, "sender_address", address, folder)
        stats["folder_aggregates"] += 1

    if category:
        _upsert_folder_aggregate(db, "category", category, folder)
        stats["folder_aggregates"] += 1

    # Subject keyword patterns
    keywords = extract_subject_keywords(email.subject or "")
    for kw in keywords[:5]:  # limit to top 5 keywords
        _upsert_folder_aggregate(db, "subject_keyword", kw, folder)
        stats["folder_aggregates"] += 1

    # Recipient-based pattern (use first recipient if available)
    if email.recipients:
        # recipients is stored as comma-separated text
        first_recipient = email.recipients.split(",")[0].strip()
        if first_recipient:
            _upsert_folder_aggregate(db, "recipient", first_recipient.lower(), folder)
            stats["folder_aggregates"] += 1

    return stats


def _update_sender_profile_for_email(
    db: Session, domain: str, folder: str, email: ProcessedEmail
) -> None:
    """Update or create a SenderProfile for the given domain."""
    profile = (
        db.query(SenderProfile)
        .filter(SenderProfile.sender_domain == domain)
        .first()
    )
    now = datetime.now(timezone.utc)

    if not profile:
        profile = SenderProfile(
            sender_domain=domain,
            total_emails=0,
            folder_distribution={},
            first_seen=now,
            last_seen=now,
        )
        db.add(profile)

    profile.total_emails = (profile.total_emails or 0) + 1
    profile.last_seen = now

    # Update folder distribution
    dist = dict(profile.folder_distribution or {})
    dist[folder] = dist.get(folder, 0) + 1
    profile.folder_distribution = dist

    # Update typical folder (most common)
    if dist:
        profile.typical_folder = max(dist, key=dist.get)

    # Update spam/importance tendencies from email flags
    if email.is_spam:
        profile.marked_spam_count = (profile.marked_spam_count or 0) + 1
    if email.is_flagged:
        profile.marked_important_count = (profile.marked_important_count or 0) + 1

    # Recalculate spam tendency
    total = profile.total_emails
    if total > 0:
        profile.spam_tendency = (profile.marked_spam_count or 0) / total
        profile.importance_tendency = (profile.marked_important_count or 0) / total

    # Update archive/delete/inbox counts from folder type
    from src.services.folder_classifier import classify_folder, FOLDER_TYPE_INBOX, FOLDER_TYPE_ARCHIVE, FOLDER_TYPE_TRASH
    folder_type = classify_folder(folder)
    if folder_type == FOLDER_TYPE_ARCHIVE:
        profile.archived_count = (profile.archived_count or 0) + 1
    elif folder_type == FOLDER_TYPE_TRASH:
        profile.deleted_count = (profile.deleted_count or 0) + 1
    elif folder_type == FOLDER_TYPE_INBOX:
        profile.kept_in_inbox_count = (profile.kept_in_inbox_count or 0) + 1

    db.add(profile)


def _upsert_folder_aggregate(
    db: Session, pattern_type: str, pattern_value: str, target_folder: str
) -> None:
    """Update or create a FolderPlacementAggregate."""
    agg = (
        db.query(FolderPlacementAggregate)
        .filter(
            FolderPlacementAggregate.pattern_type == pattern_type,
            FolderPlacementAggregate.pattern_value == pattern_value,
            FolderPlacementAggregate.target_folder == target_folder,
        )
        .first()
    )
    now = datetime.now(timezone.utc)

    if not agg:
        agg = FolderPlacementAggregate(
            pattern_type=pattern_type,
            pattern_value=pattern_value,
            target_folder=target_folder,
            occurrence_count=1,
            first_seen=now,
            last_seen=now,
        )
        db.add(agg)
    else:
        agg.occurrence_count = (agg.occurrence_count or 0) + 1
        agg.last_seen = now
        db.add(agg)

    # Recalculate total_for_pattern and confidence for ALL aggregates of this pattern
    _recalculate_pattern_confidence(db, pattern_type, pattern_value)


def _recalculate_pattern_confidence(
    db: Session, pattern_type: str, pattern_value: str
) -> None:
    """Recalculate confidence scores for all aggregates matching a pattern."""
    aggregates = (
        db.query(FolderPlacementAggregate)
        .filter(
            FolderPlacementAggregate.pattern_type == pattern_type,
            FolderPlacementAggregate.pattern_value == pattern_value,
        )
        .all()
    )
    total = sum(a.occurrence_count or 0 for a in aggregates)
    for a in aggregates:
        a.total_for_pattern = total
        a.confidence = (a.occurrence_count or 0) / total if total > 0 else 0.0
        db.add(a)


def record_user_action(
    db: Session,
    email: ProcessedEmail,
    action_type: str,
    *,
    old_folder: Optional[str] = None,
    new_folder: Optional[str] = None,
    source: str = "user",
) -> UserActionEvent:
    """Record a structured user action event for learning.

    Supported action_types:
      moved_to_folder, archived, deleted, kept_in_inbox,
      replied, forwarded, marked_important, marked_spam, unmarked_spam

    Args:
        db: Database session
        email: The email the action was performed on
        action_type: Type of action
        old_folder: Previous folder (for moves)
        new_folder: New folder (for moves)
        source: user / manual / system / imported-history

    Returns:
        The created UserActionEvent.
    """
    sender = email.sender or ""
    domain = extract_sender_domain(sender)
    subject_snip = (email.subject or "")[:200]

    event = UserActionEvent(
        email_id=email.id,
        thread_id=email.thread_id,
        action_type=action_type,
        old_folder=old_folder or email.folder,
        new_folder=new_folder,
        sender=sender,
        sender_domain=domain,
        category=email.category,
        subject_snippet=subject_snip,
        source=source,
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)

    # Also update sender profile with action info
    if domain and action_type in ("archived", "deleted", "kept_in_inbox", "marked_important", "marked_spam"):
        _update_sender_profile_for_action(db, domain, action_type)

    return event


def _update_sender_profile_for_action(db: Session, domain: str, action_type: str) -> None:
    """Update SenderProfile counters based on a user action."""
    profile = (
        db.query(SenderProfile)
        .filter(SenderProfile.sender_domain == domain)
        .first()
    )
    if not profile:
        profile = SenderProfile(
            sender_domain=domain,
            total_emails=0,
            folder_distribution={},
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        db.add(profile)

    profile.last_seen = datetime.now(timezone.utc)

    if action_type == "archived":
        profile.archived_count = (profile.archived_count or 0) + 1
    elif action_type == "deleted":
        profile.deleted_count = (profile.deleted_count or 0) + 1
    elif action_type == "kept_in_inbox":
        profile.kept_in_inbox_count = (profile.kept_in_inbox_count or 0) + 1
    elif action_type == "marked_important":
        profile.marked_important_count = (profile.marked_important_count or 0) + 1
        total = profile.total_emails or 1
        profile.importance_tendency = (profile.marked_important_count or 0) / total
    elif action_type == "marked_spam":
        profile.marked_spam_count = (profile.marked_spam_count or 0) + 1
        total = profile.total_emails or 1
        profile.spam_tendency = (profile.marked_spam_count or 0) / total

    db.add(profile)


def learn_reply_linkage(
    db: Session,
    sent_email: ProcessedEmail,
) -> Optional[Dict[str, Any]]:
    """Link a sent email back to an incoming email/thread and record reply learning signals.

    Uses headers: In-Reply-To, References, Message-ID.
    Falls back to subject/thread heuristics only if necessary (marked as heuristic).

    Args:
        db: Database session
        sent_email: A sent email to link

    Returns:
        Dict with linkage info, or None if no link found.
    """
    if not sent_email:
        return None

    # Strategy 1: Use thread_id (derived from In-Reply-To / References at ingestion time)
    linked_email = None
    linkage_method = None

    if sent_email.thread_id:
        # Find the most recent non-sent email in this thread
        linked_email = (
            db.query(ProcessedEmail)
            .filter(
                ProcessedEmail.thread_id == sent_email.thread_id,
                ProcessedEmail.id != sent_email.id,
                ProcessedEmail.folder != None,
            )
            .order_by(ProcessedEmail.date.desc())
            .first()
        )
        # Verify it's not another sent email
        if linked_email:
            folder_type = classify_folder(linked_email.folder or "")
            if folder_type == FOLDER_TYPE_SENT:
                # Skip — look for a non-sent email in the thread
                linked_email = (
                    db.query(ProcessedEmail)
                    .filter(
                        ProcessedEmail.thread_id == sent_email.thread_id,
                        ProcessedEmail.id != sent_email.id,
                    )
                    .filter(
                        ~ProcessedEmail.folder.ilike("%sent%"),
                        ~ProcessedEmail.folder.ilike("%gesendet%"),
                    )
                    .order_by(ProcessedEmail.date.desc())
                    .first()
                )
            if linked_email:
                linkage_method = "thread_id"

    # Strategy 2: Subject heuristic (only if thread_id didn't work)
    if not linked_email and sent_email.subject:
        import re
        clean_subject = re.sub(r'^(Re|Fwd|Fw|AW|WG|Antwort|Weiterleitung)\s*:\s*', '', sent_email.subject, flags=re.IGNORECASE).strip()
        if clean_subject:
            query_subj = db.query(ProcessedEmail).filter(
                ProcessedEmail.subject.ilike(f"%{clean_subject}%"),
                ProcessedEmail.id != sent_email.id,
            )
            if sent_email.date:
                query_subj = query_subj.filter(ProcessedEmail.date < sent_email.date)
            linked_email = query_subj.order_by(ProcessedEmail.date.desc()).first()
            if linked_email:
                linkage_method = "subject_heuristic"

    if not linked_email:
        return None

    # Calculate reply delay
    reply_delay_seconds = None
    if sent_email.date and linked_email.date:
        delta = sent_email.date - linked_email.date
        reply_delay_seconds = max(0.0, delta.total_seconds())

    # Update reply patterns
    original_domain = extract_sender_domain(linked_email.sender or "")
    original_category = linked_email.category or ""

    if original_domain:
        _update_reply_pattern(db, "sender_domain", original_domain, reply_delay_seconds)
    if original_category:
        _update_reply_pattern(db, "category", original_category, reply_delay_seconds)

    original_address = extract_sender_address(linked_email.sender or "")
    if original_address:
        _update_reply_pattern(db, "sender_address", original_address, reply_delay_seconds)

    # Update sender profile reply stats
    if original_domain:
        _update_sender_profile_reply(db, original_domain, reply_delay_seconds)

    result = {
        "replied_to_email_id": linked_email.id,
        "replied_to_thread_id": linked_email.thread_id,
        "reply_delay_seconds": reply_delay_seconds,
        "sender_domain": original_domain,
        "original_category": original_category,
        "original_folder": linked_email.folder,
        "linkage_method": linkage_method,
    }
    logger.debug("reply_linkage_found method=%s email_id=%s -> replied_to=%s delay=%s",
                 linkage_method, sent_email.id, linked_email.id, reply_delay_seconds)
    return result


def _update_reply_pattern(
    db: Session, pattern_type: str, pattern_value: str, reply_delay_seconds: Optional[float]
) -> None:
    """Update or create a ReplyPattern aggregate."""
    pattern = (
        db.query(ReplyPattern)
        .filter(
            ReplyPattern.pattern_type == pattern_type,
            ReplyPattern.pattern_value == pattern_value,
        )
        .first()
    )
    now = datetime.now(timezone.utc)

    if not pattern:
        pattern = ReplyPattern(
            pattern_type=pattern_type,
            pattern_value=pattern_value,
            total_received=0,
            total_replied=0,
            first_seen=now,
            last_seen=now,
        )
        db.add(pattern)

    pattern.total_replied = (pattern.total_replied or 0) + 1
    pattern.last_seen = now

    # Update delay stats
    if reply_delay_seconds is not None:
        _update_delay_stats(pattern, reply_delay_seconds)

    # Recalculate reply probability
    if pattern.total_received and pattern.total_received > 0:
        pattern.reply_probability = (pattern.total_replied or 0) / pattern.total_received
    else:
        # We don't know total_received yet, will be updated during full scan
        pattern.reply_probability = 0.0

    db.add(pattern)


def _update_delay_stats(pattern: ReplyPattern, delay_seconds: float) -> None:
    """Update delay statistics on a ReplyPattern.

    Uses running average for avg and approximation for median.
    """
    n = pattern.total_replied or 1

    # Running average
    old_avg = pattern.avg_reply_delay_seconds or delay_seconds
    pattern.avg_reply_delay_seconds = old_avg + (delay_seconds - old_avg) / n

    # Min / max
    if pattern.min_reply_delay_seconds is None or delay_seconds < pattern.min_reply_delay_seconds:
        pattern.min_reply_delay_seconds = delay_seconds
    if pattern.max_reply_delay_seconds is None or delay_seconds > pattern.max_reply_delay_seconds:
        pattern.max_reply_delay_seconds = delay_seconds

    # Approximate median (use average of min and max as rough estimate; exact median
    # would require storing all values, which we avoid for lightweight operation)
    if pattern.min_reply_delay_seconds is not None and pattern.max_reply_delay_seconds is not None:
        pattern.median_reply_delay_seconds = (
            pattern.min_reply_delay_seconds + pattern.max_reply_delay_seconds
        ) / 2.0


def _update_sender_profile_reply(
    db: Session, domain: str, reply_delay_seconds: Optional[float]
) -> None:
    """Update SenderProfile reply statistics."""
    profile = (
        db.query(SenderProfile)
        .filter(SenderProfile.sender_domain == domain)
        .first()
    )
    if not profile:
        return  # Profile should exist from email learning; skip if not

    profile.total_replies = (profile.total_replies or 0) + 1
    total = profile.total_emails or 1
    profile.reply_rate = (profile.total_replies or 0) / total

    if reply_delay_seconds is not None:
        n = profile.total_replies or 1
        old_avg = profile.avg_reply_delay_seconds or reply_delay_seconds
        profile.avg_reply_delay_seconds = old_avg + (reply_delay_seconds - old_avg) / n
        # Approximate median
        profile.median_reply_delay_seconds = profile.avg_reply_delay_seconds

    db.add(profile)


def update_reply_pattern_totals(db: Session) -> int:
    """Update total_received counts in ReplyPattern from actual email counts.

    Should be called after a full historical scan to ensure reply_probability
    is accurate.

    Returns:
        Number of patterns updated.
    """
    updated = 0

    # Update sender_domain patterns
    domain_counts = (
        db.query(
            func.lower(func.substr(ProcessedEmail.sender, func.instr(ProcessedEmail.sender, "@") + 1)),
            func.count(ProcessedEmail.id),
        )
        .filter(ProcessedEmail.sender.ilike("%@%"))
        .group_by(func.lower(func.substr(ProcessedEmail.sender, func.instr(ProcessedEmail.sender, "@") + 1)))
        .all()
    )
    for domain_val, count in domain_counts:
        if not domain_val:
            continue
        # Clean domain (remove trailing > if present)
        domain_val = domain_val.strip().rstrip(">")
        pattern = (
            db.query(ReplyPattern)
            .filter(ReplyPattern.pattern_type == "sender_domain", ReplyPattern.pattern_value == domain_val)
            .first()
        )
        if pattern:
            pattern.total_received = count
            if count > 0:
                pattern.reply_probability = (pattern.total_replied or 0) / count
            db.add(pattern)
            updated += 1

    # Update category patterns
    category_counts = (
        db.query(ProcessedEmail.category, func.count(ProcessedEmail.id))
        .filter(ProcessedEmail.category != None, ProcessedEmail.category != "")
        .group_by(ProcessedEmail.category)
        .all()
    )
    for cat, count in category_counts:
        if not cat:
            continue
        pattern = (
            db.query(ReplyPattern)
            .filter(ReplyPattern.pattern_type == "category", ReplyPattern.pattern_value == cat)
            .first()
        )
        if pattern:
            pattern.total_received = count
            if count > 0:
                pattern.reply_probability = (pattern.total_replied or 0) / count
            db.add(pattern)
            updated += 1

    db.flush()
    return updated
