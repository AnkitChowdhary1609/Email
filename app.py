"""
Mail Brain — Multi-Employee Client Onboarding Tracker
Reads Gmail across multiple employee mailboxes, groups by client,
uses Gemini to extract project/module status, and builds a live tracking dashboard.
"""
import base64, os, re, json, glob
from collections import defaultdict
from email.utils import parseaddr
from pathlib import Path

from google import genai
from google.genai import types
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from pydantic import BaseModel

load_dotenv()
SCOPES       = ["https://www.googleapis.com/auth/gmail.readonly"]
MODEL        = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
MAX_EMAILS   = int(os.getenv("MAX_EMAILS", 300))
CACHE        = "emails_cache.json"
CLIENT_CACHE = "clients_cache.json"
TOKENS_DIR   = Path("tokens")
TOKENS_DIR.mkdir(exist_ok=True)

app = FastAPI()

_api_key = os.getenv("GEMINI_API_KEY")
if not _api_key:
    raise RuntimeError("GEMINI_API_KEY not set – add it to your .env file")
gemini = genai.Client(api_key=_api_key, http_options={"api_version": "v1beta"})

# ── Load caches ──────────────────────────────────────────────
EMAILS: list[dict] = []
if os.path.exists(CACHE):
    try:
        with open(CACHE) as f:
            EMAILS = json.load(f)
    except Exception:
        EMAILS = []

CLIENTS: list[dict] = []
if os.path.exists(CLIENT_CACHE):
    try:
        with open(CLIENT_CACHE) as f:
            CLIENTS = json.load(f)
    except Exception:
        CLIENTS = []


# ── Multi-Employee Token Management ──────────────────────────

def migrate_legacy_token():
    """Migrate single token.json to tokens/ if it exists."""
    legacy = Path("token.json")
    if legacy.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(legacy), SCOPES)
            if creds and creds.valid or (creds and creds.refresh_token):
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                svc = build("gmail", "v1", credentials=creds)
                prof = svc.users().getProfile(userId="me").execute()
                email = prof.get("emailAddress", "default_employee").lower()
                clean_name = re.sub(r"[^a-zA-Z0-9_.@-]", "_", email)
                dest = TOKENS_DIR / f"{clean_name}.json"
                with open(dest, "w") as f:
                    f.write(creds.to_json())
                legacy.unlink()
        except Exception as e:
            print(f"Legacy token migration notice: {e}")


migrate_legacy_token()


def get_token_files() -> list[Path]:
    """Return all saved employee token files."""
    return sorted(list(TOKENS_DIR.glob("*.json")))


def get_gmail_service_for_token(token_path: Path):
    """
    Load credentials for an employee, automatically refreshing tokens using
    the permanent refresh_token so the employee NEVER has to log in again.
    """
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds:
        raise ValueError(f"Invalid credentials in {token_path}")
    
    if creds.expired and creds.refresh_token:
        # Silently refreshes in the background without user prompt
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())
            
    return build("gmail", "v1", credentials=creds)


def authorize_new_employee():
    """
    Runs Google OAuth with access_type='offline' and prompt='consent'
    so Google issues a permanent refresh_token that never expires.
    Saves token to tokens/{employee_email}.json
    """
    if not os.path.exists("credentials.json"):
        raise HTTPException(status_code=400, detail="credentials.json not found in project folder")
        
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    # prompt='consent' and access_type='offline' ensures a permanent refresh_token
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    
    svc = build("gmail", "v1", credentials=creds)
    prof = svc.users().getProfile(userId="me").execute()
    email = prof.get("emailAddress", f"employee_{len(get_token_files())+1}").lower()
    
    clean_name = re.sub(r"[^a-zA-Z0-9_.@-]", "_", email)
    token_file = TOKENS_DIR / f"{clean_name}.json"
    with open(token_file, "w") as f:
        f.write(creds.to_json())
        
    return {
        "email": email,
        "token_file": token_file.name,
        "status": "connected",
        "has_refresh_token": bool(creds.refresh_token)
    }


def body_text(payload) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "ignore")
    for part in payload.get("parts", []) or []:
        t = body_text(part)
        if t:
            return t
    return ""


# Well-known service domains to exclude from client tracking
SKIP_DOMAINS = {
    "gmail", "yahoo", "outlook", "hotmail", "icloud", "protonmail",
    "google", "youtube", "amazonaws", "amazon", "netflix", "spotify",
    "linkedin", "twitter", "instagram", "facebook", "apple", "microsoft",
    "github", "anthropic", "openai", "claude", "sider", "gamma",
    "doselect", "signup", "noreply", "no-reply", "mailer", "mailchimp",
    "sendgrid", "mailgun", "postmark", "zoho", "hubspot", "salesforce",
}

KNOWN_BRANDS = {
    "youtube": "YouTube", "google": "Google", "amazon": "Amazon",
    "amazonaws": "Amazon", "netflix": "Netflix", "spotify": "Spotify",
    "linkedin": "LinkedIn", "twitter": "Twitter", "instagram": "Instagram",
    "facebook": "Facebook", "apple": "Apple", "microsoft": "Microsoft",
    "github": "GitHub", "anthropic": "Anthropic", "openai": "OpenAI",
    "swiggy": "Swiggy", "zomato": "Zomato", "flipkart": "Flipkart",
}


def brand_from_sender(sender: str) -> str:
    name, addr = parseaddr(sender)
    domain = addr.split("@")[-1].lower() if "@" in addr else ""
    if not domain:
        return (name or addr or "Unknown").strip()
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net"):
        root = parts[-3]
    elif len(parts) >= 2:
        root = parts[-2]
    else:
        root = domain
    if root in ("gmail", "yahoo", "outlook", "hotmail", "icloud", "protonmail"):
        return (name or addr).strip() or "Personal"
    return KNOWN_BRANDS.get(root.lower(), root.capitalize())


def is_client_email(brand: str, sender: str) -> bool:
    """Returns True if this email is likely from a real client (not a platform/newsletter)."""
    _, addr = parseaddr(sender)
    domain = addr.split("@")[-1].lower() if "@" in addr else ""
    parts = domain.split(".")
    if len(parts) >= 2:
        root = parts[-2] if len(parts) == 2 else parts[-3] if parts[-2] in ("co","com","org","net") else parts[-2]
    else:
        root = domain
    return root not in SKIP_DOMAINS


def fetch_emails_from_all_employees() -> list[dict]:
    """
    Fetches emails across ALL connected employee mailboxes.
    Refreshes tokens automatically without re-prompting.
    Tags each email with 'handled_by': employee_email.
    """
    token_files = get_token_files()
    if not token_files:
        # Prompt for first employee authorization
        authorize_new_employee()
        token_files = get_token_files()

    all_emails = {}  # keyed by message id to deduplicate
    per_account_limit = max(50, MAX_EMAILS // max(1, len(token_files)))

    for tf in token_files:
        try:
            svc = get_gmail_service_for_token(tf)
            prof = svc.users().getProfile(userId="me").execute()
            emp_email = prof.get("emailAddress", tf.stem)

            ids, token = [], None
            while len(ids) < per_account_limit:
                r = svc.users().messages().list(
                    userId="me",
                    maxResults=min(100, per_account_limit - len(ids)),
                    pageToken=token,
                ).execute()
                ids += [m["id"] for m in r.get("messages", [])]
                token = r.get("nextPageToken")
                if not token:
                    break

            for i in ids:
                if i in all_emails:
                    continue
                try:
                    m = svc.users().messages().get(userId="me", id=i, format="full").execute()
                    h = {x["name"].lower(): x["value"] for x in m["payload"]["headers"]}
                    text = re.sub(r"\s+", " ", body_text(m["payload"]) or m.get("snippet", ""))
                    brand = brand_from_sender(h.get("from", ""))
                    all_emails[i] = {
                        "id": i,
                        "from": h.get("from", ""),
                        "to": h.get("to", ""),
                        "subject": h.get("subject", "(no subject)"),
                        "date": h.get("date", ""),
                        "brand": brand,
                        "body": text[:3000],
                        "is_client": is_client_email(brand, h.get("from", "")),
                        "handled_by": emp_email,
                    }
                except Exception as ex:
                    print(f"Error fetching message {i}: {ex}")
        except Exception as e:
            print(f"Error processing mailbox {tf.name}: {e}")

    return list(all_emails.values())


# ── AI Client Analysis ───────────────────────────────────────

ANALYSIS_PROMPT = """
You are a project manager AI analyzing email threads between a software company and their clients.
Notice that the company has multiple employees handling these client emails.

Given the following emails from/about client "{client_name}", extract a structured JSON object.

IMPORTANT: Respond with ONLY valid JSON, no markdown codeblocks, no explanation.

Schema:
{{
  "client_name": "Company name as it appears in emails",
  "contact_person": "Main client contact person name if mentioned",
  "contact_email": "Client contact email",
  "handled_by": "Name or email of the team employee handling this client",
  "overall_stage": "one of: pre_sales | kickoff_call | ops_study | uat | go_live",
  "summary": "One clear sentence summary of the onboarding/project status",
  "modules": [
    {{
      "name": "Module or feature name",
      "status": "one of: pre_sales | kickoff_call | ops_study | uat | go_live",
      "notes": "Brief notes on requirements or progress"
    }}
  ],
  "timeline": [
    {{
      "date": "Date string from email",
      "event": "Short event title (max 8 words)",
      "detail": "Brief detail of what happened",
      "actor": "client or team",
      "stage": "one of: pre_sales | kickoff_call | ops_study | uat | go_live"
    }}
  ],
  "last_activity": "Date of most recent email"
}}

Stages (Client Onboarding Lifecycle):
1. pre_sales: Initial proposals, inquiry, pre-sales scope, pricing, product demo (20% progress)
2. kickoff_call: Contract signed, kickoff call scheduled or conducted, team introduced (40% progress)
3. ops_study: Detailed operations study, workflow mapping, system architecture & development (60% progress)
4. uat: User Acceptance Testing, modules shared with client, test cases, beta feedback (80% progress)
5. go_live: Successfully launched in production, client sign-off complete (100% progress)

EMAILS:
{emails}
"""


def analyze_client(client_name: str, emails: list[dict]) -> dict:
    """Use Gemini to extract structured project data from client emails."""
    handled_by_list = sorted(list({e.get("handled_by") for e in emails if e.get("handled_by")}))
    primary_handler = ", ".join(handled_by_list) if handled_by_list else "Team"

    email_text = "\n\n---\n\n".join(
        f"Date: {e['date']}\nFrom: {e['from']}\nTo: {e.get('to', '')}\nHandled By: {e.get('handled_by', '')}\nSubject: {e['subject']}\n{e['body'][:1200]}"
        for e in emails[:25]
    )
    prompt = ANALYSIS_PROMPT.format(client_name=client_name, emails=email_text)
    try:
        r = gemini.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=3500,
                response_mime_type="application/json"
            ),
        )
        text = (r.text or "").strip()
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        data = json.loads(text)
        data["email_count"] = len(emails)
        data["brand_key"] = client_name
        if not data.get("handled_by") or data.get("handled_by") == "Team":
            data["handled_by"] = primary_handler
        return data
    except Exception as exc:
        print(f"Analysis error for {client_name}: {exc}")
        return _fallback_client(client_name, emails, primary_handler)


def _fallback_client(client_name: str, emails: list[dict], handler: str) -> dict:
    return {
        "client_name": client_name,
        "contact_email": emails[0]["from"] if emails else "",
        "contact_person": "",
        "handled_by": handler,
        "overall_stage": "ops_study",
        "summary": f"{len(emails)} emails recorded. Operations study / development in progress.",
        "modules": [],
        "timeline": [
            {"date": e["date"], "event": e["subject"][:50],
             "detail": "", "actor": "client", "stage": "ops_study"}
            for e in emails[:5]
        ],
        "last_activity": emails[0]["date"] if emails else "",
        "email_count": len(emails),
        "brand_key": client_name,
    }


# ── API endpoints ────────────────────────────────────────────

class Ask(BaseModel):
    question: str
    brand: str | None = None


@app.get("/api/accounts")
def list_accounts():
    """Returns all connected employee accounts."""
    results = []
    for tf in get_token_files():
        try:
            creds = Credentials.from_authorized_user_file(str(tf), SCOPES)
            email_display = tf.stem.replace("_", ".")
            results.append({
                "id": tf.name,
                "email": email_display,
                "valid": creds.valid or bool(creds.refresh_token),
                "has_refresh_token": bool(creds.refresh_token),
                "status": "Verified (Permanent Access)" if creds.refresh_token else "Connected"
            })
        except Exception:
            results.append({"id": tf.name, "email": tf.stem, "valid": False, "status": "Error"})
    return results


@app.post("/api/accounts/connect")
def connect_account():
    """
    Connect a new employee's mailbox.
    Once consented, permanent refresh token is saved so access is never asked again.
    """
    try:
        account_info = authorize_new_employee()
        return {"success": True, "account": account_info}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CodeRequest(BaseModel):
    code: str

@app.post("/api/accounts/connect_code")
def connect_code(payload: CodeRequest):
    """Exchanges an authorization code or saves a refresh token for permanent access."""
    import requests
    token_url = "https://oauth2.googleapis.com/token"
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    if os.path.exists("credentials.json"):
        try:
            with open("credentials.json") as f:
                cdata = json.load(f)
            ci = cdata.get("installed") or cdata.get("web") or {}
            client_id = ci.get("client_id", client_id)
            client_secret = ci.get("client_secret", client_secret)
        except Exception:
            pass

    for r_uri in ["https://emailcrawler.streamlit.app", "http://localhost", "http://localhost:8501", "urn:ietf:wg:oauth:2.0:oob"]:
        try:
            resp = requests.post(token_url, data={
                "code": payload.code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": r_uri,
                "grant_type": "authorization_code"
            })
            if resp.ok:
                tdata = resp.json()
                creds = Credentials(
                    token=tdata.get("access_token"),
                    refresh_token=tdata.get("refresh_token"),
                    token_uri=token_url,
                    client_id=client_id,
                    client_secret=client_secret,
                    scopes=SCOPES
                )
                svc = build("gmail", "v1", credentials=creds)
                prof = svc.users().getProfile(userId="me").execute()
                email = prof.get("emailAddress", "employee").lower()
                clean_name = re.sub(r"[^a-zA-Z0-9_.@-]", "_", email)
                token_file = TOKENS_DIR / f"{clean_name}.json"
                with open(token_file, "w") as f:
                    f.write(creds.to_json())
                return {"success": True, "email": email}
        except Exception as e:
            print("Code exchange error:", e)

    raise HTTPException(400, "Unable to exchange authorization code with Google")


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: str):
    """Remove a connected employee mailbox."""
    tf = TOKENS_DIR / account_id
    if tf.exists():
        tf.unlink()
        return {"success": True}
    raise HTTPException(status_code=404, detail="Account not found")


@app.post("/api/sync")
def sync():
    """Syncs across all connected employee mailboxes."""
    global EMAILS
    EMAILS = fetch_emails_from_all_employees()
    with open(CACHE, "w") as f:
        json.dump(EMAILS, f)
    return {"count": len(EMAILS), "accounts_synced": len(get_token_files())}


@app.post("/api/analyze")
def analyze():
    """AI-analyze client emails and build the tracker dashboard."""
    global CLIENTS
    if not EMAILS:
        raise HTTPException(400, "No emails loaded. Click 'Sync Mailboxes' first.")

    groups: dict[str, list] = defaultdict(list)
    for e in EMAILS:
        groups[e["brand"]].append(e)

    sorted_groups = sorted(groups.items(), key=lambda x: -len(x[1]))
    results = []
    for brand, emails in sorted_groups[:25]:
        client_data = analyze_client(brand, emails)
        results.append(client_data)

    CLIENTS = results
    with open(CLIENT_CACHE, "w") as f:
        json.dump(CLIENTS, f)
    return {"count": len(CLIENTS), "clients": CLIENTS}


@app.get("/api/clients")
def get_clients():
    return CLIENTS


@app.get("/api/brands")
def brands():
    g: dict[str, list] = defaultdict(list)
    for e in EMAILS:
        g[e["brand"]].append({k: e[k] for k in ("id", "subject", "from", "date", "brand", "body", "handled_by")})
    result = [{"brand": b, "count": len(v), "emails": v[:50]} for b, v in g.items()]
    return sorted(result, key=lambda x: -x["count"])


def pick_emails(q: str, brand: str | None, limit: int = 25) -> list[dict]:
    pool = [e for e in EMAILS if not brand or e["brand"] == brand]
    words = set(re.findall(r"\w{3,}", q.lower()))
    def score(e):
        text = (e["subject"] + " " + e["brand"] + " " + e["body"] + " " + e.get("handled_by", "")).lower()
        return sum(w in text for w in words) + (3 if e["brand"].lower() in q.lower() else 0)
    return sorted(pool, key=score, reverse=True)[:limit]


@app.post("/api/ask")
def ask(a: Ask):
    if not EMAILS:
        return {"answer": "No emails loaded yet. Click 'Sync Mailboxes' first."}
    ctx = "\n\n".join(
        f"[{e['brand']}] Handled By: {e.get('handled_by', 'Team')} | From: {e['from']} | Date: {e['date']} | Subject: {e['subject']}\n{e['body'][:1200]}"
        for e in pick_emails(a.question, a.brand)
    )
    try:
        r = gemini.models.generate_content(
            model=MODEL,
            contents=f"EMAILS:\n{ctx}\n\nQUESTION: {a.question}",
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You answer questions about the clients and projects. Be concise. "
                    "Mention client name, handling employee, stage, and dates when relevant. "
                    "If the answer isn't in the emails, say so."
                ),
                max_output_tokens=1000,
            ),
        )
        return {"answer": r.text or "No answer returned."}
    except Exception as exc:
        return {"answer": f"AI error: {exc}"}


# ── Static files ─────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    html = STATIC_DIR / "index.html"
    if not html.exists():
        raise HTTPException(404, "index.html missing from static/")
    return FileResponse(str(html))
