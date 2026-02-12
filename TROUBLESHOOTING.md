# MailJaeger Troubleshooting Guide

This guide helps you diagnose and fix common issues with MailJaeger.

## Quick Diagnostics

### System Health Check

```bash
# Check overall system health
python cli.py health

# Check system resources
free -h        # Memory usage
df -h          # Disk space
htop           # CPU and process monitoring
```

---

## Common Issues

### 1. IMAP Connection Errors

#### **Error: "Failed to connect to IMAP server"**

**Causes:**
- Wrong hostname or port
- Incorrect credentials
- Firewall blocking connection
- IMAP not enabled on email account

**Solutions:**

1. **Verify IMAP settings:**
```bash
# Check your .env file
cat .env | grep IMAP

# Common IMAP settings:
# Gmail: imap.gmail.com:993
# Outlook: outlook.office365.com:993
# Yahoo: imap.mail.yahoo.com:993
```

2. **Test IMAP connection manually:**
```bash
# Test SSL connection
openssl s_client -connect imap.gmail.com:993

# You should see "OK" in the output
```

3. **For Gmail:**
   - Enable IMAP in Gmail settings
   - Use App Password (not your regular password)
   - Create App Password: https://myaccount.google.com/apppasswords

4. **Check firewall:**
```bash
# Check if port 993 is open
nc -zv imap.gmail.com 993
```

---

### 2. AI Service Issues

#### **Error: "AI service timeout" or "AI service not available"**

**Causes:**
- Ollama not running
- Model not downloaded
- Insufficient resources
- Wrong endpoint configuration

**Solutions:**

1. **Check Ollama status:**
```bash
# Check if Ollama is running
ps aux | grep ollama

# Start Ollama if not running
ollama serve
```

2. **Verify model is downloaded:**
```bash
# List available models
ollama list

# If model is missing, pull it:
ollama pull mistral:7b-instruct-q4_0
```

3. **Test Ollama directly:**
```bash
# Test API endpoint
curl http://localhost:11434/api/tags

# Test model inference
ollama run mistral:7b-instruct-q4_0 "Hello"
```

4. **Check resources:**
```bash
# Monitor while processing emails
htop

# For Raspberry Pi 5, ensure:
# - At least 6GB free RAM
# - CPU temperature < 80°C
# - Model size fits in memory
```

5. **Adjust timeout in .env:**
```env
AI_TIMEOUT=180  # Increase to 3 minutes
```

---

### 3. Performance Issues

#### **Slow email processing on Raspberry Pi**

**Solutions:**

1. **Use smaller model:**
```bash
# Switch to more efficient model
ollama pull phi3:mini

# Update .env
AI_MODEL=phi3:mini
```

2. **Reduce batch size:**
```env
MAX_EMAILS_PER_RUN=50  # Process fewer emails per run
```

3. **Optimize storage:**
```env
STORE_EMAIL_BODY=false  # Don't store full email bodies
STORE_ATTACHMENTS=false
```

4. **Use SSD instead of SD card:**
   - Move database to SSD
   - Move search index to SSD

5. **Monitor temperature:**
```bash
# Check CPU temperature
vcgencmd measure_temp

# Add cooling if consistently > 70°C
```

---

### 4. Database Issues

#### **Error: "Database is locked"**

**Cause:** Multiple processes accessing SQLite simultaneously

**Solutions:**

1. **Stop all MailJaeger processes:**
```bash
# Find processes
ps aux | grep mailjaeger

# Kill processes
pkill -f "python.*mailjaeger"

# Or if using systemd
sudo systemctl stop mailjaeger
```

2. **Check for orphaned connections:**
```bash
# List database locks
lsof | grep mailjaeger.db
```

3. **Reset database (WARNING: deletes all data):**
```bash
rm mailjaeger.db
python cli.py init
```

#### **Error: "Disk full" or "No space left"**

**Solutions:**

1. **Check disk space:**
```bash
df -h
du -sh mailjaeger.db
du -sh search_index/
```

2. **Clean up old data:**
```bash
# Delete old logs
find logs/ -name "*.log" -mtime +30 -delete

# Compact database (inside Python)
sqlite3 mailjaeger.db "VACUUM;"
```

3. **Disable body storage:**
```env
STORE_EMAIL_BODY=false
```

---

### 5. Scheduling Issues

#### **Emails not processing automatically**

**Solutions:**

1. **Check scheduler status:**
```bash
python cli.py stats

# If using systemd
sudo systemctl status mailjaeger
sudo journalctl -u mailjaeger -f
```

2. **Verify schedule configuration:**
```bash
# Check settings
python cli.py config
```

3. **Test manual processing:**
```bash
python cli.py process
```

4. **Check timezone:**
```env
SCHEDULE_TIMEZONE=Europe/Berlin
SCHEDULE_TIME=08:00
```

---

### 6. Search Not Working

#### **Error: "Search index not found"**

**Solutions:**

1. **Rebuild search index:**
```bash
python cli.py rebuild-index
```

2. **Check index directory permissions:**
```bash
ls -la search_index/
chmod -R 755 search_index/
```

---

### 7. Memory Issues

#### **Error: "Out of memory" or system freezing**

**Solutions:**

1. **Check memory usage:**
```bash
free -h
```

2. **Reduce AI model size:**
```bash
# Use smallest model
ollama pull phi3:mini
```

3. **Limit email processing:**
```env
MAX_EMAILS_PER_RUN=25
```

4. **Add swap space (Raspberry Pi):**
```bash
# Create 4GB swap file
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Make permanent
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

### 8. Package/Dependency Errors

#### **Error: "ModuleNotFoundError"**

**Solutions:**

1. **Activate virtual environment:**
```bash
source venv/bin/activate
```

2. **Reinstall dependencies:**
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

3. **Clear pip cache:**
```bash
pip cache purge
pip install --no-cache-dir -r requirements.txt
```

---

## Logging and Debugging

### Enable Debug Mode

```env
# In .env file
DEBUG=true
LOG_LEVEL=DEBUG
```

### View Logs

```bash
# Application logs
tail -f logs/mailjaeger.log

# Systemd logs
sudo journalctl -u mailjaeger -f

# All errors
grep ERROR logs/mailjaeger.log

# Specific email processing
grep "message_id" logs/mailjaeger.log
```

### Verbose Output

```bash
# Run with verbose output
python -m src.main --log-level DEBUG
```

---

## System Requirements Check

### Minimum Requirements Checklist

- [ ] Python 3.11 installed: `python3.11 --version`
- [ ] At least 8GB RAM available: `free -h`
- [ ] At least 4GB free disk space: `df -h`
- [ ] Ollama installed: `which ollama`
- [ ] AI model downloaded: `ollama list`
- [ ] IMAP credentials configured: `cat .env | grep IMAP`
- [ ] Ports available: `nc -zv localhost 8000` and `nc -zv localhost 11434`

---

## Performance Tuning

### Raspberry Pi 5 Optimization

```bash
# 1. Overclock (carefully!)
# Edit /boot/config.txt
over_voltage=2
arm_freq=2400

# 2. Increase GPU memory for AI
# Edit /boot/config.txt
gpu_mem=256

# 3. Use fast SSD
# Move entire installation to SSD

# 4. Optimize Python
pip install --upgrade pip setuptools wheel
```

### Model Selection for Different RAM

| Available RAM | Recommended Model       | Performance |
|---------------|-------------------------|-------------|
| 4-6 GB        | phi3:mini               | Fast        |
| 6-10 GB       | llama3.2:3b             | Good        |
| 10-16 GB      | mistral:7b-instruct-q4  | Best        |

---

## Getting Help

If you can't resolve the issue:

1. **Collect diagnostic information:**
```bash
# System info
uname -a
free -h
df -h
python3.11 --version
ollama --version

# Application status
python cli.py health
python cli.py stats
python cli.py config

# Recent logs
tail -n 100 logs/mailjaeger.log
```

2. **Create GitHub issue:**
   - Go to: https://github.com/pappydee/MailJaeger/issues
   - Include diagnostic information
   - Describe steps to reproduce
   - Include error messages

3. **Check existing issues:**
   - Search: https://github.com/pappydee/MailJaeger/issues

---

## Emergency Recovery

### Complete Reset (WARNING: Deletes all data)

```bash
# Stop all services
sudo systemctl stop mailjaeger
pkill -f ollama

# Remove all data
rm -rf mailjaeger.db search_index/ logs/ attachments/

# Reinstall
./install.sh

# Reconfigure
nano .env

# Restart
sudo systemctl start mailjaeger
```

---

## Best Practices

1. **Regular backups:**
```bash
# Backup database
cp mailjaeger.db mailjaeger.db.backup

# Backup with date
cp mailjaeger.db "mailjaeger.db.$(date +%Y%m%d)"
```

2. **Monitor disk space:**
```bash
# Add to crontab
0 */6 * * * df -h | mail -s "Disk Space Report" you@example.com
```

3. **Log rotation:**
```bash
# Rotate logs weekly
find logs/ -name "*.log" -mtime +7 -exec gzip {} \;
find logs/ -name "*.gz" -mtime +30 -delete
```

4. **Regular health checks:**
```bash
# Add to crontab (daily at 6 AM)
0 6 * * * cd /home/pi/MailJaeger && python cli.py health
```
