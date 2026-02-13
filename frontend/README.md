# MailJaeger Web Dashboard

## Overview

The MailJaeger Web Dashboard is a modern, responsive single-page application that provides a user-friendly interface for managing and monitoring your email processing system.

## Features

### Dashboard Statistics
- **Total Emails**: Count of all processed emails in the system
- **Action Required**: Number of emails flagged as requiring action
- **Unresolved**: Count of action-required emails not yet resolved
- **Spam Filtered**: Number of emails identified and filtered as spam

### Processing Information
- **Last Run Status**: Success/Failure/Partial status of last processing run
- **Emails Processed**: Count of emails processed in last run
- **Error Count**: Number of failures in last run
- **Next Scheduled Run**: Date and time of next automatic processing

### System Health
- **IMAP Connection**: Status of mail server connectivity
- **AI Service**: Status of local LLM availability
- **Scheduler**: Status of automated processing scheduler

### Email Management
- **Email List**: Scrollable list of all processed emails with:
  - Subject and sender information
  - Visual priority indicators (colored bars)
  - Category badges
  - Action-required badges
  - Summary preview
  - Date and metadata

- **Email Details**: Click any email to view:
  - Full AI-generated summary
  - Complete header information
  - Extracted tasks with due dates
  - Confidence scores
  - Category classification reasoning
  - Spam probability
  - Suggested folder
  - Mark as resolved button

### Filtering
- **By Category**: Klinik, Forschung, Privat, Verwaltung, Unklar
- **By Priority**: HIGH, MEDIUM, LOW
- **By Action Status**: Action required or no action needed

### Controls
- **Manual Processing Trigger**: Button to start email processing immediately
- **Filter Application**: Apply selected filters to email list
- **Email Resolution**: Mark emails as resolved/completed

## Technical Architecture

### Frontend
- **Pure HTML5/CSS3/JavaScript**: No frameworks or build tools required
- **Responsive Design**: Works on desktop, tablet, and mobile
- **Modern CSS**: Grid layout, Flexbox, CSS variables
- **Vanilla JavaScript**: No jQuery or other dependencies

### Backend Integration
- **RESTful API**: Connects to existing FastAPI endpoints
- **Real-time Updates**: Dashboard data refreshes every 30 seconds
- **Error Handling**: Graceful degradation when services unavailable

### File Structure
```
frontend/
├── index.html     # Main dashboard HTML structure
├── style.css      # All styling (9KB)
└── app.js         # API integration and interactivity (15KB)
```

## Design System

### Colors
- **Primary**: #4F46E5 (Indigo) - Main brand color
- **Success**: #10B981 (Green) - Positive states
- **Warning**: #F59E0B (Amber) - Medium priority
- **Danger**: #EF4444 (Red) - High priority, errors
- **Background**: #F9FAFB (Light gray)
- **Surface**: #FFFFFF (White)

### Typography
- **Font**: System font stack (San Francisco, Segoe UI, Roboto)
- **Sizes**: Consistent scale from 12px to 28px
- **Weights**: 400 (normal), 600 (semibold), 700 (bold)

### Components
- **Cards**: White background with subtle shadow
- **Buttons**: Rounded corners, hover effects
- **Badges**: Small colored labels for categories
- **Icons**: SVG icons for visual clarity
- **Modal**: Overlay dialog for email details

## API Endpoints Used

### Dashboard Data
```javascript
GET /api/dashboard
```
Returns: Dashboard statistics, last run info, health status

### Email List
```javascript
POST /api/emails/list
Body: { page, page_size, filters, sort_by, sort_order }
```
Returns: Array of email objects

### Email Details
```javascript
GET /api/emails/{id}
```
Returns: Full email object with tasks

### Mark Resolved
```javascript
POST /api/emails/{id}/resolve
Body: { email_id, resolved: true }
```
Returns: Success confirmation

### Trigger Processing
```javascript
POST /api/processing/trigger
Body: { trigger_type: "MANUAL" }
```
Returns: Processing status

## Browser Compatibility

Tested and working on:
- ✅ Chrome 90+ (Windows, macOS, Linux)
- ✅ Firefox 88+
- ✅ Safari 14+
- ✅ Edge 90+
- ✅ Mobile Safari (iOS 14+)
- ✅ Chrome Mobile (Android)

## Performance

- **Initial Load**: < 1 second on local network
- **Data Refresh**: 30 second auto-refresh interval
- **API Response**: < 200ms typical response time
- **UI Updates**: Instant visual feedback

## Accessibility

- Semantic HTML structure
- ARIA labels on interactive elements
- Keyboard navigation support
- High contrast text
- Responsive font sizes

## Localization

Currently available in:
- 🇩🇪 German (default)

Easy to add translations by updating text strings in HTML and JavaScript.

## Development

### Running Locally
```bash
# Start MailJaeger server
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000

# Access dashboard
open http://localhost:8000
```

### Making Changes
1. Edit files in `frontend/` directory
2. Refresh browser to see changes (no build step)
3. Test with sample data in database

### Adding Features
1. Add HTML structure to `index.html`
2. Add styling to `style.css`
3. Add functionality to `app.js`
4. Test API integration

## Security

- **No Authentication**: Dashboard assumes local/trusted network
- **CORS Enabled**: For development (should be restricted in production)
- **No Sensitive Data**: Passwords never displayed
- **Read-Only by Default**: Most actions are read operations

### Production Recommendations
1. Add authentication (OAuth2, JWT)
2. Restrict CORS to specific origins
3. Use HTTPS with reverse proxy
4. Add rate limiting
5. Enable CSP headers

## Future Enhancements

### Planned Features
- [ ] Real-time updates via WebSocket
- [ ] Email search bar with autocomplete
- [ ] Date range picker for filtering
- [ ] Export emails to CSV/JSON
- [ ] Dark mode toggle
- [ ] Email attachment preview
- [ ] Bulk operations (mark multiple as resolved)
- [ ] Advanced statistics charts
- [ ] Email body content viewer
- [ ] Settings panel for configuration

### Possible Integrations
- [ ] Calendar integration for tasks
- [ ] Notification system
- [ ] Email composition
- [ ] Contact management
- [ ] Mobile app (React Native/Flutter)

## Troubleshooting

### Dashboard Not Loading
- Check server is running: `curl http://localhost:8000/api/health`
- Check browser console for errors
- Verify static files are being served

### Data Not Updating
- Check API endpoints: `curl http://localhost:8000/api/dashboard`
- Verify database has data
- Check browser network tab for failed requests

### Styling Issues
- Clear browser cache
- Check `style.css` is loaded: Network tab
- Verify CSS path is correct: `/static/style.css`

### JavaScript Errors
- Check browser console for errors
- Verify `app.js` is loaded: Network tab
- Check API responses are valid JSON

## Credits

**Design**: Modern dashboard inspired by contemporary web applications
**Icons**: SVG icons from Bootstrap Icons
**Colors**: Tailwind CSS color palette
**Layout**: CSS Grid and Flexbox

## License

Part of MailJaeger project - MIT License
