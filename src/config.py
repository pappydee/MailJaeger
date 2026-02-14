"""
Configuration management for MailJaeger
"""
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from typing import Optional, List
import os
from pathlib import Path


class Settings(BaseSettings):
    """Application settings"""
    
    # Application
    app_name: str = "MailJaeger"
    debug: bool = Field(default=False, description="Debug mode")
    
    # Security
    api_key: str = Field(
        default="",
        description="API authentication key(s) - REQUIRED in production. Comma-separated for multiple keys. Generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
    )
    api_key_file: Optional[str] = Field(
        default=None,
        description="Path to file containing API keys (one per line) - alternative to API_KEY env var"
    )
    trust_proxy: bool = Field(
        default=False,
        description="Trust X-Forwarded-* headers from reverse proxy (enable only when behind trusted proxy)"
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
    imap_password: str = Field(default="", description="IMAP password")
    imap_password_file: Optional[str] = Field(
        default=None,
        description="Path to file containing IMAP password (alternative to IMAP_PASSWORD)"
    )
    
    def get_imap_password(self) -> str:
        """Get IMAP password from environment or file"""
        if self.imap_password:
            return self.imap_password
        
        if self.imap_password_file:
            try:
                with open(self.imap_password_file, 'r') as f:
                    return f.read().strip()
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to load IMAP password from file: {type(e).__name__}")
                raise ValueError(f"Cannot read IMAP password from {self.imap_password_file}")
        
        return ""
    
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
    def validate_cors_origins(cls, v: str) -> List[str]:
        """Validate and parse comma-separated CORS origins"""
        if not v:
            return ["http://localhost:8000", "http://127.0.0.1:8000"]
        return [origin.strip() for origin in v.split(',') if origin.strip()]
    
    @field_validator('api_key')
    @classmethod
    def validate_api_key(cls, v: str) -> str:
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
    
    def get_api_keys(self) -> List[str]:
        """Get list of valid API keys from environment or file"""
        keys = []
        
        # Load from environment variable (comma-separated)
        if self.api_key:
            keys.extend([k.strip() for k in self.api_key.split(',') if k.strip()])
        
        # Load from file if specified
        if self.api_key_file:
            try:
                with open(self.api_key_file, 'r') as f:
                    file_keys = [line.strip() for line in f if line.strip() and not line.startswith('#')]
                    keys.extend(file_keys)
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to load API keys from file {self.api_key_file}: {type(e).__name__}")
        
        return keys
    
    def validate_required_settings(self):
        """Validate that required settings are present"""
        errors = []
        
        # Check IMAP credentials
        if not self.imap_host:
            errors.append("IMAP_HOST is required")
        if not self.imap_username:
            errors.append("IMAP_USERNAME is required")
        
        # Check IMAP password (from env or file)
        try:
            password = self.get_imap_password()
            if not password:
                errors.append("IMAP_PASSWORD or IMAP_PASSWORD_FILE is required")
        except Exception:
            errors.append("IMAP_PASSWORD or IMAP_PASSWORD_FILE is required and must be readable")
        
        # Check AI configuration
        if not self.ai_endpoint:
            errors.append("AI_ENDPOINT is required")
        if not self.ai_model:
            errors.append("AI_MODEL is required")
        
        # Security warnings (not errors)
        api_keys = self.get_api_keys()
        if not api_keys and not self.debug:
            errors.append("API_KEY not set - authentication disabled (SECURITY RISK)")
        
        if self.server_host == "0.0.0.0" and not api_keys:
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
