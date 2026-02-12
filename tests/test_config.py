"""
Unit tests for configuration
"""
import pytest
from src.config import Settings, get_settings


def test_default_settings():
    """Test default settings values"""
    settings = Settings(
        imap_host="test.example.com",
        imap_username="test@example.com",
        imap_password="testpass"
    )
    
    assert settings.app_name == "MailJaeger"
    assert settings.imap_port == 993
    assert settings.imap_use_ssl is True
    assert settings.spam_threshold == 0.7
    assert settings.max_emails_per_run == 200
    assert settings.schedule_time == "08:00"
    assert settings.ai_model == "mistral:7b-instruct-q4_0"


def test_custom_settings():
    """Test custom settings values"""
    settings = Settings(
        imap_host="custom.example.com",
        imap_username="user@example.com",
        imap_password="pass",
        spam_threshold=0.8,
        max_emails_per_run=100,
        schedule_time="09:00"
    )
    
    assert settings.imap_host == "custom.example.com"
    assert settings.spam_threshold == 0.8
    assert settings.max_emails_per_run == 100
    assert settings.schedule_time == "09:00"


def test_spam_threshold_validation():
    """Test spam threshold is within valid range"""
    settings = Settings(
        imap_host="test.example.com",
        imap_username="test@example.com",
        imap_password="testpass",
        spam_threshold=0.5
    )
    
    assert 0.0 <= settings.spam_threshold <= 1.0
