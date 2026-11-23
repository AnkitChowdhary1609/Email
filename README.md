# Mail Brain — Multi-Employee Client Onboarding Tracker

An AI-powered client onboarding and project tracking platform built on FastAPI and Google Gemini. It connects to multiple team mailboxes, parses client email threads, and builds a live, row-by-row delivery tracker with module checklists and parcel-style milestone updates.

---

## 🌟 Key Features

1. **Multi-Employee Mailbox Support**
   - Connects across different team member inboxes.
   - Permanent offline authorization: each employee verifies once via Google OAuth; refresh tokens automatically refresh access in the background without re-authenticating.

2. **Row-Wise Client Tracker**
   - Displays clients in clean, horizontal rows.
   - Shows Company, Contact, Handling Employee, Onboarding Stage, Requested Modules, and Latest Parcel Status.

3. **Stage Pipeline & Module Checklists**
   - 5-stage progression: `Onboarding` ➔ `In Progress` ➔ `Testing` ➔ `Client Confirmed` ➔ `Go Live ✓`
   - Tracks requested modules from initial request through delivery and client confirmation.

4. **Parcel-Delivery Style Updates**
   - Chronological delivery timeline with event dates, status badges, and actor tags (Client vs Team).

5. **AI Assistant**
   - Built-in Gemini chat to query client requirements, approval status, and project details across email history.

---

## 🛠️ Tech Stack

- **Backend**: FastAPI, Uvicorn, Pydantic
- **AI**: Google GenAI SDK (`gemini-2.0-flash`)
- **Gmail Integration**: Google API Python Client, Google OAuth2
- **Frontend**: Modern vanilla HTML5 / CSS3 / JavaScript

---

## 🚀 Getting Started

### 1. Prerequisites
- Python 3.10+
- Google Cloud Project with **Gmail API** enabled
- Desktop App OAuth 2.0 Credentials (`credentials.json`)
- Gemini API Key from [Google AI Studio](https://aistudio.google.com/apikey)

### 2. Setup
```bash
git clone https://github.com/ankitchowdhary2004/email.git
cd email

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Edit .env and paste your GEMINI_API_KEY
```

Place your downloaded `credentials.json` into the root directory.

### 3. Run Application
```bash
uvicorn app:app --reload
```
Open **http://localhost:8000** in your browser.

1. Click **👥 Team Mailboxes** to connect employee inboxes.
2. Click **⟳ Sync Mailboxes** to fetch emails.
3. Click **🧠 Analyze Clients** to generate the live onboarding tracker.
