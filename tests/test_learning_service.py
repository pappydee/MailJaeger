"""
Unit tests for learning service
"""

import pytest
from src.services.learning_service import LearningService


def test_extract_sender_pattern():
    """Test sender pattern extraction"""
    learning_service = LearningService(None)

    # Email addresses
    assert (
        learning_service._extract_sender_pattern("user@example.com") == "@example.com"
    )
    assert (
        learning_service._extract_sender_pattern("name <user@example.com>")
        == "@example.com"
    )

    # No email
    assert learning_service._extract_sender_pattern("No Email") == "no email"
    assert learning_service._extract_sender_pattern("") == "unknown"
    assert learning_service._extract_sender_pattern(None) == "unknown"
