# Changelog

All notable changes to MailJaeger will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2024-02-12

### Added - Initial Release

#### Core Features
- Complete IMAP email retrieval system
- AI-powered email analysis using local LLM (Ollama)
- Spam classification with AI + heuristic approach
- Automatic priority assignment (HIGH/MEDIUM/LOW)
- Task extraction with due dates and confidence scores
- Automated mailbox operations (archive, flag, spam folder)
- Learning system with pattern recognition
- Full-text search with Whoosh indexing
- RESTful API with FastAPI
- Daily scheduling system (08:00 Europe/Berlin)
- Complete audit trail and logging

#### Infrastructure
- SQLite database with SQLAlchemy ORM
- Docker and Docker Compose support
- Systemd service configuration
- Automated installation script
- CLI management tool
- Comprehensive error handling and recovery

#### Documentation
- Complete README with setup instructions
- Troubleshooting guide
- Example configurations for Gmail, Outlook, and Raspberry Pi
- Contributing guidelines
- API documentation
- Implementation summary
- MIT License

#### Testing
- Unit tests for core components
- Test infrastructure with pytest
- CodeQL security scanning (0 vulnerabilities)

#### Raspberry Pi Optimization
- Support for Mistral 7B Q4, Phi-3-mini, and Llama 3.2 3B models
- Memory-efficient configuration options
- Performance tuning for 16GB RAM
- SSD storage recommendations

### Security
- 100% local operation (no cloud services)
- No telemetry or external data transmission
- Secure credential handling
- Complete data sovereignty

### Performance
- Handles 200 emails per run
- Per-email error isolation
- Efficient search indexing
- Optimized for Raspberry Pi 5

---

## Future Releases

### Planned for v1.1.0
- Semantic search with sentence-transformers
- Retrieval-augmented summarization
- Web UI dashboard
- Enhanced attachment processing

### Planned for v2.0.0
- Multi-account support
- Calendar integration
- Mobile app
- Advanced analytics dashboard

---

[1.0.0]: https://github.com/pappydee/MailJaeger/releases/tag/v1.0.0
