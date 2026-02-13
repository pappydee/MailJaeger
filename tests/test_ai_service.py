"""
Unit tests for AI service
"""
import pytest
from src.services.ai_service import AIService


def test_validate_category():
    """Test category validation"""
    ai_service = AIService()
    
    assert ai_service._validate_category("Klinik") == "Klinik"
    assert ai_service._validate_category("Forschung") == "Forschung"
    assert ai_service._validate_category("Privat") == "Privat"
    assert ai_service._validate_category("Verwaltung") == "Verwaltung"
    assert ai_service._validate_category("Unklar") == "Unklar"
    assert ai_service._validate_category("Invalid") == "Unklar"


def test_validate_priority():
    """Test priority validation"""
    ai_service = AIService()
    
    assert ai_service._validate_priority("LOW") == "LOW"
    assert ai_service._validate_priority("MEDIUM") == "MEDIUM"
    assert ai_service._validate_priority("HIGH") == "HIGH"
    assert ai_service._validate_priority("Invalid") == "LOW"


def test_validate_tasks():
    """Test task validation"""
    ai_service = AIService()
    
    tasks = [
        {"description": "Task 1", "confidence": 0.8},
        {"description": "Task 2", "due_date": "2024-01-01", "context": "Context", "confidence": 0.9},
        {"no_description": "Invalid"},  # Invalid, no description
        {"description": ""},  # Invalid, empty description
    ]
    
    validated = ai_service._validate_tasks(tasks)
    
    assert len(validated) == 2
    assert validated[0]["description"] == "Task 1"
    assert validated[0]["confidence"] == 0.8
    assert validated[1]["description"] == "Task 2"
    assert validated[1]["due_date"] == "2024-01-01"


def test_is_spam_heuristic():
    """Test spam detection heuristics"""
    ai_service = AIService()
    
    # Spam indicators
    assert ai_service._is_spam_heuristic("unsubscribe here", "", "") is True
    assert ai_service._is_spam_heuristic("", "newsletter abmelden", "") is True
    assert ai_service._is_spam_heuristic("", "click here for free stuff", "") is True
    
    # Non-spam
    assert ai_service._is_spam_heuristic("meeting tomorrow", "lets discuss", "") is False


def test_requires_action_heuristic():
    """Test action detection heuristics"""
    ai_service = AIService()
    
    # Requires action
    assert ai_service._requires_action_heuristic("please respond", "") is True
    assert ai_service._requires_action_heuristic("", "bitte bestätigen") is True
    assert ai_service._requires_action_heuristic("urgent deadline", "") is True
    
    # No action
    assert ai_service._requires_action_heuristic("fyi", "for your information") is False


def test_fallback_classification():
    """Test fallback classification"""
    ai_service = AIService()
    
    email_data = {
        "subject": "Test email",
        "sender": "test@example.com",
        "body_plain": "This is a test email"
    }
    
    result = ai_service._fallback_classification(email_data)
    
    assert "summary" in result
    assert "category" in result
    assert result["category"] == "Unklar"
    assert "spam_probability" in result
    assert "action_required" in result
    assert "priority" in result
    assert "tasks" in result
    assert "suggested_folder" in result
    assert "reasoning" in result
