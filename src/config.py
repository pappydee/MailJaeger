"""
Configuration management for MailJaeger
"""
from pydantic_settings import BaseSettings
from pydantic import Field, validator, field_validator
from typing import Optional, List
import os
import secrets
from pathlib import Path


class Settings(BaseSettings):
    """Application settings"""
    
    # Application
    app_name: str = "MailJaeger"
    debug: bool = Field(default=False, description="Debug mode")
    
    # Security
    api_key: str = Field(
        default="",
        description="API authentication key - REQUIRED in production. Generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
    )
    
    # Server Configuration
    server_host: str = Field(
        default="127.0.0.1",
        description="Server bind address (use 127.0.0.1 for local-only, 0.0.0.0 for external)"
    )
    server_port: int = Field(default=8000, description="Server port")
    
    # CORS Configuration
    cors_origins: str = Field(
        default="http://localhost:8000,http://127.0.0.1:8000",
        description="Comma-separated list of allowed CORS origins"
    )
    
    # Database
    database_url: str = Field(
        default="sqlite:///./data/mailjaeger.db",
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
    
    # Mail Action Safety
    safe_mode: bool = Field(
        default=True,
        description="Safe mode - prevents destructive IMAP actions (dry-run)"
    )
    mark_as_read: bool = Field(
        default=False,
        description="Mark processed emails as read"
    )
    delete_spam: bool = Field(
        default=False,
        description="Delete spam emails (if false, moves to quarantine)"
    )
    quarantine_folder: str = Field(
        default="Quarantine",
        description="Quarantine folder for suspected spam"
    )
    
    # Storage
    store_email_body: bool = Field(
        default=False,
        description="Store full email body (PRIVACY: disabled by default)"
    )
    store_attachments: bool = Field(
        default=False,
        description="Store email attachments"
    )
    attachment_dir: Path = Field(
        default=Path("./data/attachments"),
        description="Directory for attachment storage"
    )
    
    # Search
    search_index_dir: Path = Field(
        default=Path("./data/search_index"),
        description="Directory for search index"
    )
    embeddings_model: str = Field(
        default="all-MiniLM-L6-v2",
        description="Model for semantic embeddings"
    )
    
    # Logging
    log_level: str = Field(default="INFO", description="Logging level")
    log_file: Optional[Path] = Field(
        default=Path("./data/logs/mailjaeger.log"),
        description="Log file path"
    )
    
    @field_validator('cors_origins')
    @classmethod
    def parse_cors_origins(cls, v: str) -> List[str]:
        """Parse comma-separated CORS origins"""
        if not v:
            return ["http://localhost:8000", "http://127.0.0.1:8000"]
        return [origin.strip() for origin in v.split(',') if origin.strip()]
    
    @field_validator('api_key')
    @classmethod
    def validate_api_key(cls, v: str, info) -> str:
        """Validate API key in production mode"""
        # Allow empty in debug mode, but warn
        if not v:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(
                "API_KEY not set! Authentication is DISABLED. "
                "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )
        return v
    
    def validate_required_settings(self):
        """Validate that required settings are present"""
        errors = []
        
        # Check IMAP credentials
        if not self.imap_host:
            errors.append("IMAP_HOST is required")
        if not self.imap_username:
            errors.append("IMAP_USERNAME is required")
        if not self.imap_password:
            errors.append("IMAP_PASSWORD is required")
        
        # Check AI configuration
        if not self.ai_endpoint:
            errors.append("AI_ENDPOINT is required")
        if not self.ai_model:
            errors.append("AI_MODEL is required")
        
        # Security warnings (not errors)
        if not self.api_key and not self.debug:
            errors.append("API_KEY not set - authentication disabled (SECURITY RISK)")
        
        if self.server_host == "0.0.0.0" and not self.api_key:
            errors.append("SERVER_HOST is 0.0.0.0 without API_KEY - publicly accessible without auth (CRITICAL SECURITY RISK)")
        
        if errors:
            raise ValueError(f"Configuration validation failed:\n" + "\n".join(f"  - {err}" for err in errors))
    
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
