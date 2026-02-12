"""
Configuration management for MailJaeger
"""
from pydantic_settings import BaseSettings
from pydantic import Field, validator
from typing import Optional
import os
from pathlib import Path


class Settings(BaseSettings):
    """Application settings"""
    
    # Application
    app_name: str = "MailJaeger"
    debug: bool = Field(default=False, description="Debug mode")
    
    # Database
    database_url: str = Field(
        default="sqlite:///./mailjaeger.db",
        description="Database connection URL"
    )
    
    # IMAP Configuration
    imap_host: str = Field(description="IMAP server hostname")
    imap_port: int = Field(default=993, description="IMAP server port")
    imap_use_ssl: bool = Field(default=True, description="Use SSL for IMAP")
    imap_username: str = Field(description="IMAP username")
    imap_password: str = Field(description="IMAP password")
    
    # Folder Configuration
    inbox_folder: str = Field(default="INBOX", description="Inbox folder name")
    archive_folder: str = Field(default="Archive", description="Archive folder name")
    spam_folder: str = Field(default="Spam", description="Spam folder name")
    
    # AI Configuration
    ai_endpoint: str = Field(
        default="http://localhost:11434",
        description="Local AI endpoint (Ollama)"
    )
    ai_model: str = Field(
        default="mistral:7b-instruct-q4_0",
        description="AI model to use (recommended: mistral:7b-instruct-q4_0, phi3:mini, or llama3.2:3b for Raspberry Pi 5)"
    )
    ai_timeout: int = Field(default=120, description="AI request timeout in seconds")
    
    # Processing Configuration
    spam_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Spam probability threshold"
    )
    max_emails_per_run: int = Field(
        default=200,
        description="Maximum emails to process per run"
    )
    
    # Scheduling
    schedule_time: str = Field(
        default="08:00",
        description="Daily schedule time (HH:MM)"
    )
    schedule_timezone: str = Field(
        default="Europe/Berlin",
        description="Timezone for scheduling"
    )
    
    # Learning System
    learning_enabled: bool = Field(
        default=True,
        description="Enable learning from user behavior"
    )
    learning_confidence_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Confidence threshold for automated folder routing"
    )
    
    # Storage
    store_email_body: bool = Field(
        default=True,
        description="Store full email body"
    )
    store_attachments: bool = Field(
        default=False,
        description="Store email attachments"
    )
    attachment_dir: Path = Field(
        default=Path("./attachments"),
        description="Directory for attachment storage"
    )
    
    # Search
    search_index_dir: Path = Field(
        default=Path("./search_index"),
        description="Directory for search index"
    )
    embeddings_model: str = Field(
        default="all-MiniLM-L6-v2",
        description="Model for semantic embeddings"
    )
    
    # Logging
    log_level: str = Field(default="INFO", description="Logging level")
    log_file: Optional[Path] = Field(
        default=Path("./logs/mailjaeger.log"),
        description="Log file path"
    )
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create settings instance"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Reload settings from environment"""
    global _settings
    _settings = Settings()
    return _settings
