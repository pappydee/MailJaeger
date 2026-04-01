"""
Folder classification utility for MailJaeger learning layer.

Classifies IMAP folder names into types:
  inbox, archive, spam, trash, sent, drafts, custom

Supports German and English folder naming conventions.
No external dependencies — pure string matching.
"""

import re
from typing import Optional


# Canonical folder types
FOLDER_TYPE_INBOX = "inbox"
FOLDER_TYPE_ARCHIVE = "archive"
FOLDER_TYPE_SPAM = "spam"
FOLDER_TYPE_TRASH = "trash"
FOLDER_TYPE_SENT = "sent"
FOLDER_TYPE_DRAFTS = "drafts"
FOLDER_TYPE_CUSTOM = "custom"

# Patterns for each folder type (lowercased substrings)
_INBOX_PATTERNS = {"inbox", "posteingang"}
_ARCHIVE_PATTERNS = {"archive", "archiv", "all mail", "alle nachrichten", "alles"}
_SPAM_PATTERNS = {"spam", "junk", "bulk", "spamverdacht"}
_TRASH_PATTERNS = {"trash", "deleted", "papierkorb", "gelöscht", "geloescht", "bin"}
_SENT_PATTERNS = {"sent", "gesendet", "gesendete"}
_DRAFTS_PATTERNS = {"drafts", "draft", "entwürfe", "entwurf", "entwuerfe"}


def classify_folder(folder_name: str) -> str:
    """Classify an IMAP folder name into a canonical type.

    Args:
        folder_name: The IMAP folder name (may include path separators like / or .)

    Returns:
        One of: inbox, archive, spam, trash, sent, drafts, custom
    """
    if not folder_name:
        return FOLDER_TYPE_CUSTOM

    # Normalize: lowercase, take last path component for matching
    normalized = folder_name.lower().strip()
    # Also check the last path component (e.g. "INBOX/Subfolder" -> "subfolder")
    parts = normalized.replace("\\", "/").replace(".", "/").split("/")
    leaf = parts[-1].strip() if parts else normalized

    # Check in priority order
    for pattern in _INBOX_PATTERNS:
        if pattern in normalized or leaf == pattern:
            return FOLDER_TYPE_INBOX

    for pattern in _SPAM_PATTERNS:
        if pattern in normalized or leaf == pattern:
            return FOLDER_TYPE_SPAM

    for pattern in _TRASH_PATTERNS:
        if pattern in normalized or leaf == pattern:
            return FOLDER_TYPE_TRASH

    for pattern in _SENT_PATTERNS:
        if pattern in normalized or leaf == pattern:
            return FOLDER_TYPE_SENT

    for pattern in _DRAFTS_PATTERNS:
        if pattern in normalized or leaf == pattern:
            return FOLDER_TYPE_DRAFTS

    for pattern in _ARCHIVE_PATTERNS:
        if pattern in normalized or leaf == pattern:
            return FOLDER_TYPE_ARCHIVE

    return FOLDER_TYPE_CUSTOM


def is_learnable_folder(folder_name: str) -> bool:
    """Check if a folder contains emails worth learning from.

    Drafts folders are excluded (incomplete emails).
    All other folder types provide useful learning signals.
    """
    folder_type = classify_folder(folder_name)
    return folder_type != FOLDER_TYPE_DRAFTS


def extract_sender_domain(sender: str) -> str:
    """Extract domain from an email sender string.

    Handles formats like:
      - "user@example.com"
      - "Name <user@example.com>"
      - "<user@example.com>"

    Returns empty string if no domain found.
    """
    if not sender:
        return ""
    sender_lower = sender.lower().strip()
    if "@" not in sender_lower:
        return ""
    # Extract from angle brackets if present
    if "<" in sender_lower and ">" in sender_lower:
        start = sender_lower.index("<") + 1
        end = sender_lower.index(">")
        sender_lower = sender_lower[start:end].strip()
    at_idx = sender_lower.rfind("@")
    if at_idx < 0:
        return ""
    domain = sender_lower[at_idx + 1:].strip().rstrip(">")
    return domain


def extract_sender_address(sender: str) -> str:
    """Extract bare email address from sender string.

    Handles formats like:
      - "user@example.com"
      - "Name <user@example.com>"
    """
    if not sender:
        return ""
    sender_lower = sender.lower().strip()
    if "<" in sender_lower and ">" in sender_lower:
        start = sender_lower.index("<") + 1
        end = sender_lower.index(">")
        return sender_lower[start:end].strip()
    if "@" in sender_lower:
        return sender_lower
    return ""


def extract_subject_keywords(subject: str, min_length: int = 3) -> list[str]:
    """Extract significant keywords from a subject for pattern matching.

    Strips common prefixes (Re:, Fwd:, AW:, WG:) and stop words.
    Returns lowercased keywords of at least min_length characters.
    """
    if not subject:
        return []

    # Remove common prefixes
    cleaned = re.sub(
        r"^(Re|Fwd|Fw|AW|WG|Antwort|Weiterleitung)\s*:\s*",
        "",
        subject,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.strip()

    # Split into words, filter short ones
    words = re.split(r"\W+", cleaned.lower())

    # Common stop words (German + English)
    stop_words = {
        "the", "and", "for", "are", "but", "not", "you", "all",
        "can", "her", "was", "one", "our", "out", "von", "und",
        "der", "die", "das", "den", "dem", "des", "ein", "eine",
        "ist", "hat", "mit", "auf", "für", "fuer", "bei", "zum",
        "zur", "vom", "ihr", "sie", "wir", "ich", "aus", "nach",
        "über", "ueber", "wie", "was", "bis", "nur",
    }

    return [w for w in words if len(w) >= min_length and w not in stop_words]
