#!/usr/bin/env python3
"""
MailJaeger CLI - Command-line interface for management operations
"""
import sys
import argparse
import logging
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import get_settings
from src.database.connection import init_db, get_db_session
from src.services.email_processor import EmailProcessor
from src.services.search_service import SearchService
from src.utils.logging import setup_logging

logger = logging.getLogger(__name__)


def init_command(args):
    """Initialize MailJaeger"""
    print("Initializing MailJaeger...")
    setup_logging()
    init_db()
    print("✓ Database initialized")
    print("✓ MailJaeger is ready!")
    print(f"\nNext steps:")
    print(f"1. Edit .env file with your IMAP credentials")
    print(f"2. Start Ollama: ollama serve")
    print(f"3. Pull a model: ollama pull mistral:7b-instruct-q4_0")
    print(f"4. Run the application: python -m src.main")


def process_command(args):
    """Manually trigger email processing"""
    print("Starting email processing...")
    setup_logging()
    init_db()
    
    with get_db_session() as db:
        processor = EmailProcessor(db)
        run = processor.process_emails(trigger_type="MANUAL")
        
        print(f"\nProcessing completed!")
        print(f"Status: {run.status}")
        print(f"Emails processed: {run.emails_processed}")
        print(f"Spam detected: {run.emails_spam}")
        print(f"Archived: {run.emails_archived}")
        print(f"Action required: {run.emails_action_required}")
        print(f"Failed: {run.emails_failed}")


def rebuild_index_command(args):
    """Rebuild search index"""
    print("Rebuilding search index...")
    setup_logging()
    init_db()
    
    with get_db_session() as db:
        search_service = SearchService(db)
        search_service.rebuild_index()
    
    print("✓ Search index rebuilt")


def stats_command(args):
    """Show statistics"""
    setup_logging()
    init_db()
    
    from src.models.database import ProcessedEmail, ProcessingRun
    
    with get_db_session() as db:
        total_emails = db.query(ProcessedEmail).count()
        spam_count = db.query(ProcessedEmail).filter(ProcessedEmail.is_spam == True).count()
        action_count = db.query(ProcessedEmail).filter(
            ProcessedEmail.action_required == True,
            ProcessedEmail.is_spam == False
        ).count()
        unresolved_count = db.query(ProcessedEmail).filter(
            ProcessedEmail.action_required == True,
            ProcessedEmail.is_resolved == False,
            ProcessedEmail.is_spam == False
        ).count()
        
        total_runs = db.query(ProcessingRun).count()
        last_run = db.query(ProcessingRun).order_by(ProcessingRun.started_at.desc()).first()
        
        print("\n=== MailJaeger Statistics ===\n")
        print(f"Total emails: {total_emails}")
        print(f"Spam emails: {spam_count}")
        print(f"Action required: {action_count}")
        print(f"Unresolved: {unresolved_count}")
        print(f"\nTotal processing runs: {total_runs}")
        
        if last_run:
            print(f"\nLast run:")
            print(f"  Time: {last_run.started_at}")
            print(f"  Status: {last_run.status}")
            print(f"  Processed: {last_run.emails_processed}")


def health_command(args):
    """Check system health"""
    print("Checking system health...")
    setup_logging()
    
    from src.services.imap_service import IMAPService
    from src.services.ai_service import AIService
    
    # IMAP Health
    imap = IMAPService()
    imap_health = imap.check_health()
    print(f"\n📧 IMAP: {imap_health['status']}")
    print(f"   {imap_health['message']}")
    
    # AI Health
    ai = AIService()
    ai_health = ai.check_health()
    print(f"\n🤖 AI Service: {ai_health['status']}")
    print(f"   {ai_health['message']}")
    
    # Database Health
    try:
        init_db()
        print(f"\n💾 Database: healthy")
        print(f"   Database operational")
    except Exception as e:
        print(f"\n💾 Database: unhealthy")
        print(f"   {str(e)}")


def config_command(args):
    """Show current configuration"""
    settings = get_settings()
    
    print("\n=== MailJaeger Configuration ===\n")
    print(f"IMAP Host: {settings.imap_host}")
    print(f"IMAP Port: {settings.imap_port}")
    print(f"IMAP Username: {settings.imap_username}")
    print(f"AI Endpoint: {settings.ai_endpoint}")
    print(f"AI Model: {settings.ai_model}")
    print(f"Spam Threshold: {settings.spam_threshold}")
    print(f"Schedule: {settings.schedule_time} {settings.schedule_timezone}")
    print(f"Learning Enabled: {settings.learning_enabled}")
    print(f"Database: {settings.database_url}")


def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description="MailJaeger - Local AI email processing system"
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Init command
    init_parser = subparsers.add_parser('init', help='Initialize MailJaeger')
    init_parser.set_defaults(func=init_command)
    
    # Process command
    process_parser = subparsers.add_parser('process', help='Process emails manually')
    process_parser.set_defaults(func=process_command)
    
    # Rebuild index command
    rebuild_parser = subparsers.add_parser('rebuild-index', help='Rebuild search index')
    rebuild_parser.set_defaults(func=rebuild_index_command)
    
    # Stats command
    stats_parser = subparsers.add_parser('stats', help='Show statistics')
    stats_parser.set_defaults(func=stats_command)
    
    # Health command
    health_parser = subparsers.add_parser('health', help='Check system health')
    health_parser.set_defaults(func=health_command)
    
    # Config command
    config_parser = subparsers.add_parser('config', help='Show configuration')
    config_parser.set_defaults(func=config_command)
    
    args = parser.parse_args()
    
    if hasattr(args, 'func'):
        try:
            args.func(args)
        except Exception as e:
            print(f"\n❌ Error: {e}")
            if '--debug' in sys.argv:
                raise
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
