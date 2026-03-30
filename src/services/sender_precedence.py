"""
Centralized sender-precedence helpers for MailJaeger.

Provides a single authoritative implementation of the address > domain
fallback logic used across prediction, importance scoring, and learning.

Precedence tiers (most specific first):
  1. sender_address  — exact email address
  2. sender_domain   — domain-level fallback
  3. (category / subject_keyword — handled by callers where applicable)

This module eliminates duplicated address/domain lookup logic that was
previously inlined in prediction_engine.py, importance_scorer.py, and
historical_learning.py.
"""

from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from src.models.database import (
    FolderPlacementAggregate,
    ReplyPattern,
    SenderProfile,
)
from src.services.folder_classifier import extract_sender_address, extract_sender_domain


# ---------------------------------------------------------------------------
# SenderProfile resolution (address > domain)
# ---------------------------------------------------------------------------

def resolve_sender_profile(
    db: Session,
    sender: str,
    *,
    min_support: int = 3,
) -> Optional[SenderProfile]:
    """Return the most specific SenderProfile with sufficient support.

    Tries address-level first; falls back to domain-level (sender_address IS NULL).
    Returns *None* when no profile meets *min_support*.
    """
    address = extract_sender_address(sender)
    domain = extract_sender_domain(sender)

    if address:
        profile = (
            db.query(SenderProfile)
            .filter(SenderProfile.sender_address == address)
            .first()
        )
        if profile and (profile.total_emails or 0) >= min_support:
            return profile

    if domain:
        profile = (
            db.query(SenderProfile)
            .filter(
                SenderProfile.sender_domain == domain,
                SenderProfile.sender_address.is_(None),
            )
            .first()
        )
        if profile and (profile.total_emails or 0) >= min_support:
            return profile

    return None


def resolve_sender_profile_label(
    db: Session,
    sender: str,
    *,
    min_support: int = 3,
) -> Tuple[Optional[SenderProfile], str]:
    """Like *resolve_sender_profile* but also returns a human-friendly label.

    Returns ``(profile, label)`` where *label* is the address or domain
    string that matched, or an empty string when *profile* is None.
    """
    address = extract_sender_address(sender)
    domain = extract_sender_domain(sender)

    if address:
        profile = (
            db.query(SenderProfile)
            .filter(SenderProfile.sender_address == address)
            .first()
        )
        if profile and (profile.total_emails or 0) >= min_support:
            return profile, address

    if domain:
        profile = (
            db.query(SenderProfile)
            .filter(
                SenderProfile.sender_domain == domain,
                SenderProfile.sender_address.is_(None),
            )
            .first()
        )
        if profile and (profile.total_emails or 0) >= min_support:
            return profile, domain

    return None, ""


# ---------------------------------------------------------------------------
# Prediction-tier builders (folder / reply)
# ---------------------------------------------------------------------------

def build_folder_tiers(
    sender: str, category: str, subject_keywords: List[str]
) -> List[Tuple[str, str]]:
    """Build the ordered list of ``(pattern_type, pattern_value)`` tiers
    for folder-placement prediction.

    Tier order:
      1. sender_address
      2. sender_domain
      3. category
      4. subject_keyword (first 3)
    """
    address = extract_sender_address(sender)
    domain = extract_sender_domain(sender)

    tiers: List[Tuple[str, str]] = []
    if address:
        tiers.append(("sender_address", address))
    if domain:
        tiers.append(("sender_domain", domain))
    if category:
        tiers.append(("category", category))
    for kw in subject_keywords[:3]:
        tiers.append(("subject_keyword", kw))
    return tiers


def build_reply_tiers(
    sender: str, category: str
) -> List[Tuple[str, str]]:
    """Build the ordered list of ``(pattern_type, pattern_value)`` tiers
    for reply-needed prediction.

    Tier order:
      1. sender_address
      2. sender_domain
      3. category
    """
    address = extract_sender_address(sender)
    domain = extract_sender_domain(sender)

    tiers: List[Tuple[str, str]] = []
    if address:
        tiers.append(("sender_address", address))
    if domain:
        tiers.append(("sender_domain", domain))
    if category:
        tiers.append(("category", category))
    return tiers
