# Contributing to MailJaeger

Thank you for your interest in contributing to MailJaeger! This document provides guidelines and instructions for contributing.

## Code of Conduct

By participating in this project, you agree to maintain a respectful and inclusive environment for all contributors.

## How to Contribute

### Reporting Issues

Before creating an issue:
1. Check if the issue already exists
2. Collect diagnostic information (see TROUBLESHOOTING.md)
3. Provide clear reproduction steps

**Good Issue Template:**
```
**Environment:**
- OS: Raspberry Pi OS / Ubuntu / etc.
- Python version: 3.11.x
- MailJaeger version: 1.0.0
- AI Model: mistral:7b-instruct-q4_0

**Description:**
Clear description of the issue

**Steps to Reproduce:**
1. Step 1
2. Step 2
3. ...

**Expected Behavior:**
What should happen

**Actual Behavior:**
What actually happens

**Logs:**
```
Relevant log output
```
```

### Suggesting Features

For feature requests:
1. Check if it aligns with project goals (privacy-first, local-only)
2. Describe the use case
3. Explain expected behavior
4. Consider implementation complexity

### Pull Requests

1. **Fork the repository**
2. **Create a feature branch:**
   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Make your changes:**
   - Follow the code style (PEP 8 for Python)
   - Add tests for new functionality
   - Update documentation as needed
   - Keep commits focused and atomic

4. **Test your changes:**
   ```bash
   # Run tests
   pytest tests/
   
   # Check code style
   black src/
   flake8 src/
   
   # Test manually
   python cli.py health
   python cli.py process
   ```

5. **Commit with clear messages:**
   ```bash
   git commit -m "Add: Feature description"
   git commit -m "Fix: Issue description"
   git commit -m "Docs: Documentation update"
   ```

6. **Push and create Pull Request:**
   ```bash
   git push origin feature/your-feature-name
   ```

7. **PR Description should include:**
   - What changes were made
   - Why the changes were needed
   - How to test the changes
   - Related issue numbers

## Development Setup

### Prerequisites
- Python 3.11+
- Ollama with a model installed
- Access to an IMAP email account for testing

### Setup Development Environment

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/MailJaeger.git
cd MailJaeger

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Setup test environment
cp .env.example .env.test
# Edit .env.test with test credentials

# Run tests
pytest tests/
```

## Code Style

### Python
- Follow PEP 8
- Use type hints where appropriate
- Maximum line length: 100 characters
- Use meaningful variable names
- Add docstrings to functions and classes

**Example:**
```python
def process_email(email_data: Dict[str, Any]) -> ProcessedEmail:
    """
    Process a single email through the analysis pipeline.
    
    Args:
        email_data: Dictionary containing email data
        
    Returns:
        ProcessedEmail: The processed email record
        
    Raises:
        ValueError: If email_data is invalid
    """
    pass
```

### Formatting Tools

```bash
# Auto-format code
black src/

# Check style
flake8 src/

# Sort imports
isort src/
```

## Testing

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src tests/

# Run specific test file
pytest tests/test_ai_service.py

# Run with verbose output
pytest -v
```

### Writing Tests

- Place tests in `tests/` directory
- Name test files `test_*.py`
- Name test functions `test_*`
- Use descriptive test names
- Test both success and failure cases

**Example:**
```python
def test_spam_classification_high_threshold():
    """Test spam classification with high confidence"""
    ai_service = AIService()
    result = ai_service._classify_spam(
        email_data={'subject': 'Free money click here'},
        analysis={'spam_probability': 0.95}
    )
    assert result is True
```

## Project Structure

```
MailJaeger/
├── src/
│   ├── api/              # API endpoints (future)
│   ├── config.py         # Configuration management
│   ├── database/         # Database connection
│   ├── models/           # Data models
│   ├── services/         # Business logic
│   ├── utils/            # Utilities
│   └── main.py           # Application entry point
├── tests/                # Test suite
├── examples/             # Example configurations
├── cli.py                # CLI tool
└── docs/                 # Documentation
```

## Areas for Contribution

### High Priority
- [ ] Web UI dashboard
- [ ] Semantic search implementation
- [ ] Retrieval-augmented summarization
- [ ] Attachment analysis
- [ ] Performance optimizations

### Medium Priority
- [ ] Multi-account support
- [ ] Calendar integration
- [ ] Custom categorization rules
- [ ] Export/import functionality
- [ ] Backup/restore tools

### Low Priority
- [ ] Mobile app
- [ ] Browser extension
- [ ] Email templates
- [ ] Statistics and reporting
- [ ] Notification system

## Documentation

When adding features:
1. Update README.md if user-facing
2. Update API documentation
3. Add examples if applicable
4. Update TROUBLESHOOTING.md for common issues

## Release Process

1. Version numbering follows Semantic Versioning (MAJOR.MINOR.PATCH)
2. Update version in `src/__init__.py`
3. Update CHANGELOG.md
4. Tag release: `git tag v1.0.0`
5. Push tag: `git push origin v1.0.0`

## Questions?

- Open a GitHub Discussion
- Create an issue labeled "question"
- Check existing documentation

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

Thank you for contributing to MailJaeger! 🎉
