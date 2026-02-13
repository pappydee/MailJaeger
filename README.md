# MailJaeger

**MailJaeger** is a fully local, privacy-first AI email processing system that autonomously analyzes, structures, prioritizes, archives, and organizes incoming emails. It operates continuously with minimal manual oversight and functions as a structured decision-support layer above a standard IMAP mailbox.

## Key Features

- **🔒 100% Local & Private**: All processing occurs locally, no cloud AI services or telemetry
- **🤖 AI-Powered Analysis**: Automatic email categorization, priority assessment, and task extraction
- **🎯 Smart Filtering**: Intelligent spam detection and action-required identification
- **📊 Structured Organization**: Automatic archiving with learned folder suggestions
- **🧠 Continuous Learning**: Adapts to your behavior and improves over time
- **🔍 Powerful Search**: Full-text and semantic search with filtering
- **📅 Automated Processing**: Scheduled daily runs with manual trigger option
- **🌐 Web Dashboard**: Modern web interface for email management and monitoring
- **🛠️ RESTful API**: Complete API for custom integrations

## System Requirements

### Minimum Requirements
- **Raspberry Pi 5** (16GB RAM recommended) or equivalent Linux system
- **4GB free disk space** (more if storing email bodies and attachments)
- **IMAP email account** (Gmail, Outlook, or any IMAP-compatible service)

### Recommended for Raspberry Pi 5
- Use **Mistral 7B Q4** (4GB RAM) or **Phi-3-mini** (2-3GB RAM) for optimal performance
- Install on SSD for better performance than SD card

## Installation

### 1. Install System Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11 and dependencies
sudo apt install -y python3.11 python3.11-venv python3-pip git

# Install Ollama (local LLM server)
curl -fsSL https://ollama.com/install.sh | sh
```

### 2. Clone Repository

```bash
git clone https://github.com/pappydee/MailJaeger.git
cd MailJaeger
```

### 3. Setup Python Environment

```bash
# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Install and Configure Local LLM

Choose one of the following models based on your preference:

```bash
# Option 1: Mistral 7B Q4 (recommended for best quality, ~4GB RAM)
ollama pull mistral:7b-instruct-q4_0

# Option 2: Phi-3-mini (most efficient, ~2-3GB RAM)
ollama pull phi3:mini

# Option 3: Llama 3.2 3B (good alternative, ~2-3GB RAM)
ollama pull llama3.2:3b

# Start Ollama service
ollama serve
```

### 5. Configure Environment

```bash
# Copy example configuration
cp .env.example .env

# Edit configuration with your settings
nano .env
```

**Required Configuration:**
```env
# IMAP Settings (example for Gmail)
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USERNAME=your_email@gmail.com
IMAP_PASSWORD=your_app_password  # Use app-specific password for Gmail

# AI Model (choose one you pulled)
AI_MODEL=mistral:7b-instruct-q4_0

# Adjust other settings as needed
```

**For Gmail:** You need to create an [App Password](https://support.google.com/accounts/answer/185833)

### 6. Initialize Database

```bash
# The database will be created automatically on first run
python -m src.main
```

## Usage

### Start the Application

```bash
# Activate virtual environment
source venv/bin/activate

# Start the application
python -m src.main
```

The application will be available at `http://localhost:8000`

### Web Dashboard

Access the web dashboard at: **http://localhost:8000**

The dashboard provides:
- 📊 Real-time statistics (total emails, action required, spam filtered)
- 📧 Email list with filtering and sorting
- 🔍 Detailed email view with AI analysis and tasks
- ⚡ Manual processing trigger
- 💚 System health monitoring

### API Documentation

Interactive API documentation available at:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

### Key Endpoints

- `GET /` - Web Dashboard (NEW!)
- `GET /api/dashboard` - Overview dashboard with statistics
- `POST /api/emails/list` - List emails with filters
- `GET /api/emails/{id}` - Get email details
- `POST /api/emails/search` - Search emails
- `POST /api/processing/trigger` - Manually trigger processing
- `GET /api/processing/runs` - Get processing history
- `GET /api/health` - System health check

## How It Works

### Email Processing Workflow

1. **Retrieval**: Connects to IMAP and fetches unread emails
2. **AI Analysis**: Each email is analyzed for:
   - German summary
   - Category (Klinik, Forschung, Privat, Verwaltung, Unklar)
   - Spam probability
   - Action required status
   - Priority (LOW, MEDIUM, HIGH)
   - Extracted tasks with due dates
   - Suggested folder
3. **Spam Classification**: Combines AI analysis with heuristics
4. **Mailbox Actions**: 
   - Spam → Moved to Spam folder
   - Non-spam → Marked read, moved to Archive
   - Action required → Flagged
5. **Persistence**: Stored in local database with full audit trail
6. **Learning**: System learns from folder movements and improves suggestions

### Automated Scheduling

- **Default**: Runs daily at 08:00 (Europe/Berlin timezone)
- **Configurable**: Change `SCHEDULE_TIME` in `.env`
- **Manual Trigger**: Use API or run manually

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        MailJaeger                           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐            │
│  │   IMAP   │───▶│    AI    │───▶│ Database │            │
│  │ Service  │    │ Analysis │    │ (SQLite) │            │
│  └──────────┘    └──────────┘    └──────────┘            │
│                                                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐            │
│  │ Learning │    │  Search  │    │Scheduler │            │
│  │  System  │    │  Engine  │    │ Service  │            │
│  └──────────┘    └──────────┘    └──────────┘            │
│                                                             │
│  ┌─────────────────────────────────────────────┐          │
│  │           FastAPI REST API                  │          │
│  └─────────────────────────────────────────────┘          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                         ▲
                         │
                  ┌──────┴──────┐
                  │   Ollama    │
                  │  (Local LLM)│
                  └─────────────┘
```

## Performance Optimization

### For Raspberry Pi 5

1. **Use SSD instead of SD card** for database and search index
2. **Adjust batch size**: Set `MAX_EMAILS_PER_RUN` to 50-100 if processing is slow
3. **Reduce AI timeout**: Set `AI_TIMEOUT=60` if model responds faster
4. **Disable body storage**: Set `STORE_EMAIL_BODY=false` to save space
5. **Monitor resources**: Use `htop` to check CPU/memory usage

```bash
# Monitor system resources
htop

# Check Ollama logs
journalctl -u ollama -f
```

## Security & Privacy

- ✅ **No cloud services**: Everything runs locally
- ✅ **No telemetry**: Zero external communication except IMAP
- ✅ **Encrypted credentials**: Passwords never logged
- ✅ **Audit trail**: Complete record of all actions
- ✅ **Data sovereignty**: Your data stays on your device

## Troubleshooting

### IMAP Connection Issues

```bash
# Test IMAP connection
openssl s_client -connect imap.gmail.com:993

# Check credentials in .env
cat .env | grep IMAP
```

### AI Service Not Responding

```bash
# Check Ollama status
ollama list

# Restart Ollama
sudo systemctl restart ollama

# Check if model is loaded
curl http://localhost:11434/api/tags
```

### Database Issues

```bash
# Reset database (WARNING: deletes all data)
rm mailjaeger.db
python -m src.main
```

### Performance Issues

```bash
# Check system resources
free -h
df -h
ps aux | grep python

# Reduce concurrent processing
# Edit .env and set MAX_EMAILS_PER_RUN=50
```

## Development

### Running in Development Mode

```bash
# Enable debug logging
export DEBUG=true
export LOG_LEVEL=DEBUG

# Run with auto-reload
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

### Project Structure

```
MailJaeger/
├── src/
│   ├── api/              # API endpoints
│   ├── config.py         # Configuration management
│   ├── database/         # Database connection
│   ├── models/           # Data models
│   │   ├── database.py   # SQLAlchemy models
│   │   └── schemas.py    # Pydantic schemas
│   ├── services/         # Business logic
│   │   ├── ai_service.py
│   │   ├── email_processor.py
│   │   ├── imap_service.py
│   │   ├── learning_service.py
│   │   ├── scheduler.py
│   │   └── search_service.py
│   ├── utils/            # Utilities
│   └── main.py           # FastAPI application
├── requirements.txt      # Python dependencies
├── .env.example          # Example configuration
└── README.md             # This file
```

## Roadmap

Version 1.0 includes all core features as specified. Future enhancements:
- [ ] Web UI dashboard
- [ ] Mobile app support
- [ ] Calendar integration
- [ ] Attachment analysis
- [ ] Multi-language support beyond German
- [ ] Export/import functionality

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For issues and questions:
- **GitHub Issues**: https://github.com/pappydee/MailJaeger/issues
- **Documentation**: https://github.com/pappydee/MailJaeger/wiki

## Acknowledgments

Built with:
- [FastAPI](https://fastapi.tiangolo.com/) - Modern web framework
- [Ollama](https://ollama.ai/) - Local LLM serving
- [SQLAlchemy](https://www.sqlalchemy.org/) - Database ORM
- [Whoosh](https://whoosh.readthedocs.io/) - Full-text search
- [IMAPClient](https://imapclient.readthedocs.io/) - IMAP library