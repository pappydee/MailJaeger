"""
Learning Loop Service for MailJaeger.

Handles:
  - Recording manual user classifications as decision events
  - Updating SenderProfile with user-preferred category/folder
  - Looking up sender-based learning info
  - Rule-based pre-classification using known sender profiles and heuristics

The learning loop is:
  user decision → DecisionEvent stored → SenderProfile updated → reused on future emails
"""

from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.database import (
    ProcessedEmail,
    SenderProfile,
    DecisionEvent,
)
from src.services.folder_classifier import extract_sender_domain, extract_sender_address
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Categories available for manual classification
VALID_CATEGORIES = {"work", "private", "newsletter", "todo", "spam"}

# Simple heuristic patterns for newsletter detection
_NEWSLETTER_SENDER_PATTERNS = [
    "noreply@",
    "no-reply@",
    "newsletter@",
    "news@",
    "digest@",
    "mailer@",
    "marketing@",
    "updates@",
    "notifications@",
    "info@",
    "team@",
    "hello@",
]

_NEWSLETTER_HEADER_PATTERNS = [
    "list-unsubscribe",
    "unsubscribe",
    "newsletter",
    "digest",
    "weekly update",
    "monthly update",
]


def record_manual_classification(
    db: Session,
    email: ProcessedEmail,
    category: str,
    target_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a manual user classification and update the learning loop.

    Steps:
      1. Update the email's category (and suggested_folder if provided)
      2. Create a DecisionEvent with source=user_manual
      3. Update sender profile with preferred category/folder

    Returns dict with result info.
    """
    category = category.lower()
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category: {category}. Must be one of {VALID_CATEGORIES}")

    sender = email.sender or ""
    old_category = email.category

    # 1. Update email classification
    email.category = category
    if target_folder:
        email.suggested_folder = target_folder
    email.reasoning = f"Manually classified by user as '{category}'"
    if not email.overridden:
        email.original_classification = {
            "category": old_category,
            "priority": email.priority,
            "suggested_folder": email.suggested_folder if not target_folder else None,
        }
    email.overridden = True

    # Handle spam classification
    if category == "spam":
        email.is_spam = True
        email.spam_probability = 0.95
    elif old_category and old_category.lower() == "spam":
        # Un-marking as spam
        email.is_spam = False
        email.spam_probability = 0.05

    # 2. Record decision event
    subject_snippet = (email.subject or "")[:200]
    event = DecisionEvent(
        email_id=email.id,
        thread_id=email.thread_id,
        event_type="manual_classify",
        source="user_manual",
        old_value=old_category,
        new_value=category,
        sender=sender,
        subject_snippet=subject_snippet,
        chosen_category=category,
        chosen_folder=target_folder,
        user_confirmed=True,
        confidence=1.0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)

    # 3. Update sender profiles
    sender_updated = _update_sender_learning(db, sender, category, target_folder)

    return {
        "email_id": email.id,
        "category": category,
        "target_folder": target_folder,
        "sender_profile_updated": sender_updated,
        "old_category": old_category,
    }


def _update_sender_learning(
    db: Session,
    sender: str,
    category: str,
    target_folder: Optional[str],
) -> bool:
    """Update sender profile with user-preferred category and folder.

    Updates both address-level and domain-level profiles.
    Returns True if any profile was updated.
    """
    if not sender:
        return False

    domain = extract_sender_domain(sender)
    address = extract_sender_address(sender)
    updated = False

    # Update address-level profile
    if address:
        profile = _get_or_create_sender_profile(db, sender_address=address)
        _apply_user_classification_to_profile(profile, category, target_folder)
        updated = True

    # Update domain-level profile
    if domain:
        profile = _get_or_create_sender_profile(db, sender_domain=domain)
        _apply_user_classification_to_profile(profile, category, target_folder)
        updated = True

    return updated


def _get_or_create_sender_profile(
    db: Session,
    *,
    sender_address: Optional[str] = None,
    sender_domain: Optional[str] = None,
) -> SenderProfile:
    """Get or create a SenderProfile for the given address or domain."""
    if sender_address:
        profile = (
            db.query(SenderProfile)
            .filter(SenderProfile.sender_address == sender_address)
            .first()
        )
        if not profile:
            domain = sender_address.split("@")[-1] if "@" in sender_address else None
            profile = SenderProfile(
                sender_address=sender_address,
                sender_domain=domain,
            )
            db.add(profile)
            db.flush()
    elif sender_domain:
        profile = (
            db.query(SenderProfile)
            .filter(
                SenderProfile.sender_domain == sender_domain,
                SenderProfile.sender_address.is_(None),
            )
            .first()
        )
        if not profile:
            profile = SenderProfile(
                sender_domain=sender_domain,
                sender_address=None,
            )
            db.add(profile)
            db.flush()
    else:
        raise ValueError("Either sender_address or sender_domain must be provided")

    return profile


def _apply_user_classification_to_profile(
    profile: SenderProfile,
    category: str,
    target_folder: Optional[str],
) -> None:
    """Apply a user classification decision to a sender profile."""
    profile.preferred_category = category
    if target_folder:
        profile.preferred_folder = target_folder
    profile.user_classification_count = (profile.user_classification_count or 0) + 1
    profile.last_seen = datetime.now(timezone.utc)
    profile.updated_at = datetime.now(timezone.utc)

    # Update spam tendency based on classification
    if category == "spam":
        profile.spam_tendency = min(1.0, (profile.spam_tendency or 0.0) + 0.3)
    elif (profile.spam_tendency or 0.0) > 0.0:
        profile.spam_tendency = max(0.0, (profile.spam_tendency or 0.0) - 0.1)


def get_sender_learning_info(
    db: Session,
    sender: str,
) -> Dict[str, Any]:
    """Look up learning info for a sender (address-level first, then domain)."""
    address = extract_sender_address(sender)
    domain = extract_sender_domain(sender)

    # Try address-level first (higher precedence)
    profile = None
    if address:
        profile = (
            db.query(SenderProfile)
            .filter(SenderProfile.sender_address == address)
            .first()
        )

    # Fall back to domain-level
    if not profile and domain:
        profile = (
            db.query(SenderProfile)
            .filter(
                SenderProfile.sender_domain == domain,
                SenderProfile.sender_address.is_(None),
            )
            .first()
        )

    if not profile:
        return {
            "sender": sender,
            "preferred_category": None,
            "preferred_folder": None,
            "user_classification_count": 0,
            "total_emails": 0,
            "typical_folder": None,
        }

    return {
        "sender": sender,
        "preferred_category": profile.preferred_category,
        "preferred_folder": profile.preferred_folder,
        "user_classification_count": profile.user_classification_count or 0,
        "total_emails": profile.total_emails or 0,
        "typical_folder": profile.typical_folder,
    }


# ── Rule-based pre-classification ───────────────────────────────────────


def rule_based_classify(
    db: Session,
    email: ProcessedEmail,
) -> Optional[Dict[str, Any]]:
    """Attempt to classify an email using deterministic rules.

    Returns a dict with classification info if a rule matched, or None
    if no rule applies (fallback to LLM).

    Rule priority:
      1. Known sender profile (address-level, then domain-level)
      2. Newsletter pattern heuristics
      3. Spam pattern heuristics
    """
    sender = email.sender or ""

    # Rule 1: Known sender profile
    result = _classify_by_sender_profile(db, sender)
    if result:
        return result

    # Rule 2: Newsletter detection
    result = _classify_newsletter(sender, email.subject)
    if result:
        return result

    # Rule 3: Spam heuristics
    result = _classify_spam_heuristic(sender, email.subject)
    if result:
        return result

    return None


def _classify_by_sender_profile(
    db: Session,
    sender: str,
) -> Optional[Dict[str, Any]]:
    """Classify using known sender profile (highest precedence)."""
    address = extract_sender_address(sender)
    domain = extract_sender_domain(sender)

    # Try address-level first
    profile = None
    if address:
        profile = (
            db.query(SenderProfile)
            .filter(SenderProfile.sender_address == address)
            .first()
        )

    # Fall back to domain-level
    if not profile and domain:
        profile = (
            db.query(SenderProfile)
            .filter(
                SenderProfile.sender_domain == domain,
                SenderProfile.sender_address.is_(None),
            )
            .first()
        )

    if not profile:
        return None

    # Check if sender has high spam probability from historical learning
    spam_prob = getattr(profile, "spam_probability", None)
    if spam_prob is not None and float(spam_prob or 0) >= 0.8:
        return {
            "category": "spam",
            "suggested_folder": None,
            "explanation": "Historical learning: sender has high spam probability",
            "confidence": min(1.0, float(spam_prob)),
            "source": "sender_spam_profile",
        }

    # Only use the profile if user has explicitly classified it
    if (profile.user_classification_count or 0) > 0 and profile.preferred_category:
        return {
            "category": profile.preferred_category,
            "suggested_folder": profile.preferred_folder or profile.typical_folder,
            "explanation": "Learned from previous user classification",
            "confidence": min(1.0, 0.7 + 0.1 * (profile.user_classification_count or 0)),
            "source": "sender_profile",
        }

    # If sender has a strong folder pattern from history, use it for folder suggestion
    if profile.typical_folder and (profile.total_emails or 0) >= 3:
        return {
            "category": None,  # don't override category from history alone
            "suggested_folder": profile.typical_folder,
            "explanation": "Known sender rule",
            "confidence": 0.5,
            "source": "sender_history",
        }

    return None


def _classify_newsletter(
    sender: str,
    subject: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Detect obvious newsletters using sender/subject heuristics."""
    sender_lower = sender.lower()
    subject_lower = (subject or "").lower()

    # Check sender patterns
    for pattern in _NEWSLETTER_SENDER_PATTERNS:
        if pattern in sender_lower:
            return {
                "category": "newsletter",
                "suggested_folder": None,
                "explanation": "Newsletter pattern detected",
                "confidence": 0.7,
                "source": "newsletter_heuristic",
            }

    # Check subject patterns
    for pattern in _NEWSLETTER_HEADER_PATTERNS:
        if pattern in subject_lower:
            return {
                "category": "newsletter",
                "suggested_folder": None,
                "explanation": "Newsletter pattern detected",
                "confidence": 0.6,
                "source": "newsletter_heuristic",
            }

    return None


def _classify_spam_heuristic(
    sender: str,
    subject: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Basic spam heuristics for obvious cases."""
    subject_lower = (subject or "").lower()

    spam_phrases = [
        "you have won",
        "claim your prize",
        "limited time offer",
        "act now",
        "congratulations you",
        "earn money fast",
    ]
    for phrase in spam_phrases:
        if phrase in subject_lower:
            return {
                "category": "spam",
                "suggested_folder": None,
                "explanation": "Spam pattern detected",
                "confidence": 0.6,
                "source": "spam_heuristic",
            }

    return None
