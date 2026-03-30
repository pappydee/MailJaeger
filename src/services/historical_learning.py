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
    ReplyLink,
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
        _update_sender_profile_for_email(db, domain, None, folder, email)
        stats["sender_profiles"] += 1

    # Update sender profile (address-level)
    if address:
        _update_sender_profile_for_email(db, None, address, folder, email)
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
    db: Session, domain: str | None, address: str | None, folder: str, email: ProcessedEmail
) -> None:
    """Update or create a SenderProfile for the given domain or address.

    Exactly one of domain/address should be set.  When address is set a
    per-address profile is maintained; when domain is set a per-domain
    profile is maintained.
    """
    if address:
        profile = (
            db.query(SenderProfile)
            .filter(SenderProfile.sender_address == address)
            .first()
        )
    else:
        profile = (
            db.query(SenderProfile)
            .filter(
                SenderProfile.sender_domain == domain,
                SenderProfile.sender_address.is_(None),
            )
            .first()
        )
    now = datetime.now(timezone.utc)

    if not profile:
        profile = SenderProfile(
            sender_domain=domain if domain else extract_sender_domain(address or ""),
            sender_address=address,
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

    # Update sender profiles at both address-level and domain-level
    address = extract_sender_address(sender)
    learnable_actions = ("archived", "deleted", "kept_in_inbox", "marked_important", "marked_spam")
    if action_type in learnable_actions:
        if domain:
            _update_sender_profile_for_action(db, domain=domain, address=None, action_type=action_type)
        if address:
            _update_sender_profile_for_action(db, domain=None, address=address, action_type=action_type)

    return event


def _get_or_create_sender_profile(
    db: Session, *, domain: Optional[str] = None, address: Optional[str] = None
) -> SenderProfile:
    """Get or create a SenderProfile for a given domain or address.

    Exactly one of domain/address should be provided.
    - When domain is set (address is None): returns the domain-level profile.
    - When address is set: returns the address-level profile.
    """
    now = datetime.now(timezone.utc)
    if address:
        profile = (
            db.query(SenderProfile)
            .filter(SenderProfile.sender_address == address)
            .first()
        )
        if not profile:
            profile = SenderProfile(
                sender_domain=extract_sender_domain(address),
                sender_address=address,
                total_emails=0,
                folder_distribution={},
                first_seen=now,
                last_seen=now,
            )
            db.add(profile)
    else:
        profile = (
            db.query(SenderProfile)
            .filter(
                SenderProfile.sender_domain == domain,
                SenderProfile.sender_address.is_(None),
            )
            .first()
        )
        if not profile:
            profile = SenderProfile(
                sender_domain=domain,
                total_emails=0,
                folder_distribution={},
                first_seen=now,
                last_seen=now,
            )
            db.add(profile)
    return profile


def _apply_action_to_profile(profile: SenderProfile, action_type: str) -> None:
    """Apply action-type counter increments to a SenderProfile."""
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


def _update_sender_profile_for_action(
    db: Session, *, domain: Optional[str] = None, address: Optional[str] = None, action_type: str
) -> None:
    """Update SenderProfile counters based on a user action.

    Exactly one of domain/address should be set.
    """
    profile = _get_or_create_sender_profile(db, domain=domain, address=address)
    _apply_action_to_profile(profile, action_type)
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

    # Update sender profile reply stats (both domain and address levels)
    if original_domain:
        _update_sender_profile_reply(db, original_domain, reply_delay_seconds,
                                     address=original_address)

    # Persist durable reply link (deduplicated by unique constraint)
    _confidence_map = {"thread_id": 0.9, "subject_heuristic": 0.4}
    _persist_reply_link(
        db,
        sent_email_id=sent_email.id,
        original_email_id=linked_email.id,
        thread_id=linked_email.thread_id or sent_email.thread_id,
        linkage_method=linkage_method or "unknown",
        confidence=_confidence_map.get(linkage_method, 0.5),
        reply_delay_seconds=reply_delay_seconds,
        original_sender_domain=original_domain,
        original_category=original_category,
        original_folder=linked_email.folder,
    )

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
    db: Session, domain: str, reply_delay_seconds: Optional[float],
    address: Optional[str] = None,
) -> None:
    """Update SenderProfile reply statistics for both domain and address levels."""
    # Update domain-level profile
    profile = (
        db.query(SenderProfile)
        .filter(
            SenderProfile.sender_domain == domain,
            SenderProfile.sender_address.is_(None),
        )
        .first()
    )
    if profile:
        _apply_reply_stats(profile, reply_delay_seconds)
        db.add(profile)

    # Update address-level profile if available
    if address:
        addr_profile = (
            db.query(SenderProfile)
            .filter(SenderProfile.sender_address == address)
            .first()
        )
        if addr_profile:
            _apply_reply_stats(addr_profile, reply_delay_seconds)
            db.add(addr_profile)


def _apply_reply_stats(profile: SenderProfile, reply_delay_seconds: Optional[float]) -> None:
    """Apply reply statistics to a SenderProfile (domain or address level)."""
    profile.total_replies = (profile.total_replies or 0) + 1
    total = profile.total_emails or 1
    profile.reply_rate = (profile.total_replies or 0) / total

    if reply_delay_seconds is not None:
        n = profile.total_replies or 1
        old_avg = profile.avg_reply_delay_seconds or reply_delay_seconds
        profile.avg_reply_delay_seconds = old_avg + (reply_delay_seconds - old_avg) / n
        # Approximate median
        profile.median_reply_delay_seconds = profile.avg_reply_delay_seconds


def _persist_reply_link(
    db: Session,
    *,
    sent_email_id: int,
    original_email_id: int,
    thread_id: Optional[str],
    linkage_method: str,
    confidence: float,
    reply_delay_seconds: Optional[float],
    original_sender_domain: str,
    original_category: str,
    original_folder: Optional[str],
) -> Optional[ReplyLink]:
    """Persist a durable reply-link record, deduplicated by (sent, original) pair."""
    existing = (
        db.query(ReplyLink)
        .filter(
            ReplyLink.sent_email_id == sent_email_id,
            ReplyLink.original_email_id == original_email_id,
        )
        .first()
    )
    if existing:
        return existing  # already stored — no duplicate

    link = ReplyLink(
        sent_email_id=sent_email_id,
        original_email_id=original_email_id,
        thread_id=thread_id,
        linkage_method=linkage_method,
        confidence=confidence,
        reply_delay_seconds=reply_delay_seconds,
        original_sender_domain=original_sender_domain,
        original_category=original_category,
        original_folder=original_folder,
    )
    db.add(link)
    return link


def update_reply_pattern_totals(db: Session) -> int:
    """Update total_received counts in ReplyPattern from actual email counts.

    Should be called after a full historical scan to ensure reply_probability
    is accurate.  Handles sender_domain, sender_address, and category tiers.

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

    # Update sender_address patterns
    address_counts = (
        db.query(
            func.lower(ProcessedEmail.sender),
            func.count(ProcessedEmail.id),
        )
        .filter(ProcessedEmail.sender.ilike("%@%"))
        .group_by(func.lower(ProcessedEmail.sender))
        .all()
    )
    for addr_val, count in address_counts:
        if not addr_val:
            continue
        # Normalize: extract bare address from "Name <addr>" format
        addr_clean = extract_sender_address(addr_val)
        if not addr_clean:
            continue
        pattern = (
            db.query(ReplyPattern)
            .filter(ReplyPattern.pattern_type == "sender_address", ReplyPattern.pattern_value == addr_clean)
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
