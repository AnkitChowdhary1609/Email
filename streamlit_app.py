import os, re, json, glob, base64
from pathlib import Path
from email.utils import parseaddr
from collections import defaultdict
import streamlit as st
from dotenv import load_dotenv

# Google & Gemini imports
from google import genai
from google.genai import types
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

# Page configuration
st.set_page_config(
    page_title="Mail Brain — Client Onboarding Tracker",
    page_icon="📬",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Paths & Constants (Absolute paths) ────────────────────────
BASE_DIR     = Path(__file__).resolve().parent
SCOPES       = ["https://www.googleapis.com/auth/gmail.readonly"]
CACHE        = BASE_DIR / "emails_cache.json"
CLIENT_CACHE = BASE_DIR / "clients_cache.json"
TOKENS_DIR   = BASE_DIR / "tokens"
TOKENS_DIR.mkdir(exist_ok=True)

# ── Secrets Resolver (Works for Cloud Secrets, .env, and UI inputs) ──
def get_secret(key, default=""):
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)

GEMINI_API_KEY = get_secret("GEMINI_API_KEY")
MODEL          = get_secret("GEMINI_MODEL", "gemini-2.0-flash")
MAX_EMAILS     = int(get_secret("MAX_EMAILS", "300"))

# Check for Gemini API Key
if not GEMINI_API_KEY:
    st.error("⚠️ `GEMINI_API_KEY` is not set. Please add it to your Streamlit Cloud Secrets or `.env` file.")
    st.info("In Streamlit Cloud: Go to App Settings ➔ Secrets ➔ Add `GEMINI_API_KEY = '...'`")
    st.stop()

@st.cache_resource
def get_gemini_client():
    return genai.Client(api_key=GEMINI_API_KEY, http_options={"api_version": "v1beta"})

gemini = get_gemini_client()


# ── Custom CSS for Modern Row-Wise Dashboard ──────────────────
st.markdown("""
<style>
    [data-testid="stMetricValue"] {
        font-size: 1.8rem;
        font-weight: 800;
    }
    .client-card-row {
        background: #131722;
        border: 1px solid #263045;
        border-radius: 12px;
        padding: 16px 20px;
        margin-bottom: 12px;
        transition: all .2s;
    }
    .client-card-row:hover {
        border-color: #7c6af7;
        box-shadow: 0 4px 16px rgba(124, 106, 247, 0.12);
    }
    .handler-badge {
        display: inline-block;
        background: #232b40;
        color: #56cfb2;
        padding: 3px 10px;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
        margin-top: 4px;
    }
    .stage-badge {
        display: inline-block;
        padding: 3px 12px;
        border-radius: 999px;
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .badge-onboarding { background: #1e3a8a; color: #93c5fd; border: 1px solid #3b82f6; }
    .badge-in_progress { background: #451a03; color: #fcd34d; border: 1px solid #f59e0b; }
    .badge-testing { background: #3b0764; color: #d8b4fe; border: 1px solid #a855f7; }
    .badge-client_confirmed { background: #064e3b; color: #6ee7b7; border: 1px solid #10b981; }
    .badge-go_live { background: #164e63; color: #67e8f9; border: 1px solid #06b6d4; }
    .mod-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: #1a2030;
        border: 1px solid #263045;
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 0.8rem;
        margin: 3px 4px 3px 0;
    }
    .mod-chip.done { border-color: #10b981; color: #6ee7b7; }
    .mod-chip.inprog { border-color: #f59e0b; color: #fcd34d; }
    .mod-chip.sent { border-color: #a855f7; color: #d8b4fe; }
    .timeline-box {
        background: #1a2030;
        border-left: 3px solid #7c6af7;
        padding: 10px 14px;
        border-radius: 0 8px 8px 0;
        margin-bottom: 8px;
    }
</style>
""", unsafe_allow_html=True)


# ── Multi-Source Gmail Credential Resolver (Cloud, UI & Local) ─

if "custom_accounts" not in st.session_state:
    st.session_state.custom_accounts = []


def get_all_employee_credentials() -> list[dict]:
    """
    Finds credentials in priority:
    1. UI added accounts (st.session_state.custom_accounts)
    2. Streamlit Cloud Secrets ([employee_1], [employee_2], etc.)
    3. Flat Streamlit Secrets (GMAIL_CLIENT_ID, GMAIL_REFRESH_TOKEN)
    4. Local tokens/ directory (*.json)
    """
    accounts = []

    # 1. Accounts added dynamically via UI
    for acc in st.session_state.custom_accounts:
        if not any(a["email"] == acc["email"] for a in accounts):
            accounts.append(acc)

    # 2. Check Streamlit Secrets (nested tables [employee_1], [employee_2], [google_oauth])
    if hasattr(st, "secrets"):
        for key in st.secrets:
            try:
                sec = st.secrets[key]
                if isinstance(sec, dict) and "refresh_token" in sec and "client_id" in sec:
                    email = sec.get("email", key)
                    if not any(a["email"] == email for a in accounts):
                        accounts.append({
                            "email": email,
                            "source": "secrets",
                            "client_id": sec["client_id"],
                            "client_secret": sec.get("client_secret", ""),
                            "refresh_token": sec["refresh_token"],
                        })
            except Exception:
                pass

        # 3. Check flat Streamlit Secrets (GMAIL_CLIENT_ID, GMAIL_REFRESH_TOKEN)
        if "GMAIL_REFRESH_TOKEN" in st.secrets and "GMAIL_CLIENT_ID" in st.secrets:
            email = st.secrets.get("GMAIL_USER_EMAIL", "default_employee")
            if not any(a["email"] == email for a in accounts):
                accounts.append({
                    "email": email,
                    "source": "secrets",
                    "client_id": st.secrets["GMAIL_CLIENT_ID"],
                    "client_secret": st.secrets.get("GMAIL_CLIENT_SECRET", ""),
                    "refresh_token": st.secrets["GMAIL_REFRESH_TOKEN"],
                })

    # 4. Check local tokens/ directory
    for tf in TOKENS_DIR.glob("*.json"):
        try:
            d = json.load(open(tf))
            if d.get("refresh_token") and d.get("client_id"):
                email = tf.stem.replace("_", ".")
                if not any(a["email"] == email for a in accounts):
                    accounts.append({
                        "email": email,
                        "source": "file",
                        "token_path": tf,
                        "client_id": d["client_id"],
                        "client_secret": d.get("client_secret", ""),
                        "refresh_token": d["refresh_token"],
                    })
        except Exception:
            pass

    return accounts


def build_gmail_service_for_account(acc: dict):
    """
    Builds a Gmail API service using the permanent refresh_token.
    Automatically refreshes expired access tokens in the background with zero user prompts.
    """
    creds = Credentials(
        token=None,
        refresh_token=acc["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=acc["client_id"],
        client_secret=acc["client_secret"],
        scopes=SCOPES
    )
    if not creds.valid or creds.expired:
        creds.refresh(Request())
        if acc.get("source") == "file" and acc.get("token_path"):
            try:
                with open(acc["token_path"], "w") as f:
                    f.write(creds.to_json())
            except Exception:
                pass

    return build("gmail", "v1", credentials=creds)


def body_text(payload) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "ignore")
    for part in payload.get("parts", []) or []:
        t = body_text(part)
        if t:
            return t
    return ""


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
    _, addr = parseaddr(sender)
    domain = addr.split("@")[-1].lower() if "@" in addr else ""
    parts = domain.split(".")
    root = parts[-2] if len(parts) >= 2 else domain
    return root not in SKIP_DOMAINS


def fetch_emails_across_mailboxes() -> list[dict]:
    accounts = get_all_employee_credentials()
    if not accounts:
        st.error("No Gmail accounts found.")
        return []

    all_emails = {}
    per_account_limit = max(50, MAX_EMAILS // max(1, len(accounts)))

    for acc in accounts:
        try:
            svc = build_gmail_service_for_account(acc)
            emp_email = acc["email"]

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
                except Exception:
                    pass
        except Exception as e:
            st.warning(f"Error accessing {acc['email']}: {e}")

    emails_list = list(all_emails.values())
    try:
        with open(CACHE, "w") as f:
            json.dump(emails_list, f)
    except Exception:
        pass
    return emails_list


# ── AI Client Onboarding Analysis ────────────────────────────

ANALYSIS_PROMPT = """
You are a project manager AI analyzing email threads between a software company and their clients.
Notice that multiple employees handle different client accounts.

Given the following emails from/about client "{client_name}", extract a structured JSON object.

Schema:
{{
  "client_name": "Company name",
  "contact_person": "Main client contact person",
  "contact_email": "Client contact email",
  "handled_by": "Name or email of team employee handling this client",
  "overall_stage": "one of: onboarding | in_progress | testing | client_confirmed | go_live",
  "summary": "One clear sentence summary of the project/onboarding status",
  "modules": [
    {{
      "name": "Module or feature name",
      "status": "one of: requested | in_development | update_sent | client_confirmed | go_live",
      "notes": "Brief notes on status"
    }}
  ],
  "timeline": [
    {{
      "date": "Date string",
      "event": "Short event title (max 8 words)",
      "detail": "Brief detail",
      "actor": "client or team",
      "stage": "one of: requested | in_development | update_sent | client_confirmed | go_live"
    }}
  ]
}}

EMAILS:
{emails}
"""

def analyze_client(client_name: str, emails: list[dict]) -> dict:
    handlers = list({e.get("handled_by") for e in emails if e.get("handled_by")})
    primary_handler = handlers[0] if handlers else "Team"

    email_text = "\n\n---\n\n".join(
        f"Date: {e['date']}\nFrom: {e['from']}\nHandled By: {e.get('handled_by', '')}\nSubject: {e['subject']}\n{e['body'][:1200]}"
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
        data = json.loads(text)
        data["email_count"] = len(emails)
        data["brand_key"] = client_name
        if not data.get("handled_by") or data.get("handled_by") == "Team":
            data["handled_by"] = primary_handler
        return data
    except Exception as exc:
        return {
            "client_name": client_name,
            "contact_email": emails[0]["from"] if emails else "",
            "contact_person": "",
            "handled_by": primary_handler,
            "overall_stage": "in_progress",
            "summary": f"{len(emails)} emails recorded. Status in progress.",
            "modules": [],
            "timeline": [
                {"date": e["date"], "event": e["subject"][:50], "detail": "", "actor": "client", "stage": "requested"}
                for e in emails[:4]
            ],
            "email_count": len(emails),
            "brand_key": client_name,
        }

def run_full_analysis(emails: list[dict]):
    groups = defaultdict(list)
    for e in emails:
        if e.get("is_client"):
            groups[e["brand"]].append(e)
    if not groups:
        for e in emails:
            groups[e["brand"]].append(e)

    sorted_groups = sorted(groups.items(), key=lambda x: -len(x[1]))[:25]
    results = []
    prog_bar = st.progress(0, text="Analyzing client communications...")
    total = len(sorted_groups)

    for idx, (brand, brand_emails) in enumerate(sorted_groups):
        prog_bar.progress((idx + 1) / total, text=f"Analyzing {brand} ({idx+1}/{total})...")
        c_data = analyze_client(brand, brand_emails)
        results.append(c_data)

    prog_bar.empty()
    try:
        with open(CLIENT_CACHE, "w") as f:
            json.dump(results, f)
    except Exception:
        pass
    return results


# ── Load Session State ────────────────────────────────────────
if "emails" not in st.session_state:
    if os.path.exists(CACHE):
        try:
            st.session_state.emails = json.load(open(CACHE))
        except Exception:
            st.session_state.emails = []
    else:
        st.session_state.emails = []

if "clients" not in st.session_state:
    if os.path.exists(CLIENT_CACHE):
        try:
            st.session_state.clients = json.load(open(CLIENT_CACHE))
        except Exception:
            st.session_state.clients = []
    else:
        st.session_state.clients = []


# ── SIDEBAR ───────────────────────────────────────────────────
with st.sidebar:
    st.title("📬 Mail Brain")
    st.caption("Multi-Employee Client Onboarding Tracker")
    st.divider()

    # Sync Mailboxes
    if st.button("⟳ Sync Team Mailboxes", use_container_width=True, type="secondary"):
        with st.spinner("Fetching emails across all connected mailboxes..."):
            fetched = fetch_emails_across_mailboxes()
            st.session_state.emails = fetched
            st.success(f"Synced {len(fetched)} emails!")

    # Analyze Clients
    if st.button("🧠 Analyze Clients (AI)", use_container_width=True, type="primary"):
        if not st.session_state.emails:
            st.warning("Please click 'Sync Team Mailboxes' first.")
        else:
            with st.spinner("Running Gemini AI onboarding analysis..."):
                analyzed = run_full_analysis(st.session_state.emails)
                st.session_state.clients = analyzed
                st.success(f"Generated tracking for {len(analyzed)} clients!")
                st.rerun()

    st.divider()

    # Connected Employee Accounts Display
    st.subheader("👥 Connected Mailboxes")
    accounts = get_all_employee_credentials()
    if accounts:
        for acc in accounts:
            st.markdown(f"🟢 **{acc['email']}**")
    else:
        st.warning("No mailboxes configured.")

    # In-App Add / Connect Employee Mailbox Form
    with st.expander("➕ Connect Mailbox Directly"):
        st.caption("Enter your Gmail OAuth credentials:")
        inp_email = st.text_input("Employee Email", placeholder="user@gmail.com")
        inp_refresh = st.text_input("Refresh Token", placeholder="1//...", type="password")
        inp_cid = st.text_input("Client ID", placeholder="...apps.googleusercontent.com")
        inp_csec = st.text_input("Client Secret", placeholder="GOCSPX-...", type="password")
        if st.button("Connect & Save", use_container_width=True):
            if inp_email and inp_refresh and inp_cid:
                st.session_state.custom_accounts.append({
                    "email": inp_email,
                    "refresh_token": inp_refresh,
                    "client_id": inp_cid,
                    "client_secret": inp_csec,
                    "source": "ui"
                })
                st.success(f"Connected {inp_email}!")
                st.rerun()
            else:
                st.error("Please fill in email, refresh token, and client id.")

    st.divider()

    # Client search filter
    search_q = st.text_input("🔍 Filter Clients", "").strip().lower()


# ── DASHBOARD METRICS ─────────────────────────────────────────
clients = st.session_state.clients
total_c = len(clients)
inprog_c = len([c for c in clients if c.get("overall_stage") in ("in_progress", "testing", "onboarding")])
confirmed_c = len([c for c in clients if c.get("overall_stage") == "client_confirmed"])
golive_c = len([c for c in clients if c.get("overall_stage") == "go_live"])

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Clients", total_c)
col2.metric("In Development / Testing", inprog_c)
col3.metric("Confirmed by Client", confirmed_c)
col4.metric("Go-Live ✓", golive_c)

st.divider()


# ── ROW-WISE CLIENT TRACKER ───────────────────────────────────
STAGE_MAP = {
    "onboarding":       {"label": "Onboarding",        "pct": 0.15, "badge": "badge-onboarding"},
    "in_progress":      {"label": "In Progress",       "pct": 0.40, "badge": "badge-in_progress"},
    "testing":          {"label": "Testing",           "pct": 0.65, "badge": "badge-testing"},
    "client_confirmed": {"label": "Client Confirmed",  "pct": 0.85, "badge": "badge-client_confirmed"},
    "go_live":          {"label": "Go Live ✓",         "pct": 1.00, "badge": "badge-go_live"},
}

MOD_STATUS_ICONS = {
    "requested":        ("⬜", "Requested", ""),
    "in_development":   ("⚙️", "In Dev", "inprog"),
    "update_sent":      ("↗️", "Update Sent", "sent"),
    "client_confirmed": ("✅", "Confirmed", "done"),
    "go_live":          ("🚀", "Live", "done"),
}

filtered_clients = [
    c for c in clients
    if not search_q or search_q in (c.get("client_name", "") + " " + c.get("brand_key", "")).lower()
]

if not filtered_clients:
    if not clients:
        st.info("👋 Ready to sync! Click **'⟳ Sync Team Mailboxes'** and then **'🧠 Analyze Clients (AI)'** in the sidebar to populate your live tracker.")
        
        # If no accounts are detected yet, show setup guide
        if not accounts:
            st.markdown("""
            ### 🔑 Quick Setup for Streamlit Cloud:
            Go to **App Settings ➔ Secrets** in Streamlit Cloud and paste your secrets to start syncing automatically.
            """)
    else:
        st.info("No clients match your filter.")
else:
    st.subheader(f"📋 Client Onboarding Rows ({len(filtered_clients)})")

    for c in filtered_clients:
        stage_key = c.get("overall_stage", "in_progress")
        stage_info = STAGE_MAP.get(stage_key, STAGE_MAP["in_progress"])
        modules = c.get("modules", [])
        timeline = c.get("timeline", [])
        client_title = c.get("client_name") or c.get("brand_key")
        handler = c.get("handled_by", "Team")

        with st.container():
            st.markdown(f"""
            <div class="client-card-row">
                <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                    <div>
                        <span style="font-size:1.25rem; font-weight:800; color:#f1f5f9;">{client_title}</span>
                        <div style="color:#94a3b8; font-size:0.84rem; margin-top:2px;">
                            ✉️ {c.get("contact_email") or "—"} {f"· 👤 {c.get('contact_person')}" if c.get('contact_person') else ""}
                        </div>
                        <span class="handler-badge">👤 Handled by: {handler}</span>
                    </div>
                    <div style="text-align:right;">
                        <span class="stage-badge {stage_info['badge']}">{stage_info['label']}</span>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            rcol1, rcol2 = st.columns([1, 2])
            with rcol1:
                st.caption(f"Stage Progress: **{int(stage_info['pct']*100)}%**")
                st.progress(stage_info["pct"])
                st.markdown(f"*{c.get('summary', 'Status in progress.')}*")

            with rcol2:
                st.caption("Requested Modules:")
                if modules:
                    mod_chips_html = "".join([
                        f"""<span class="mod-chip {MOD_STATUS_ICONS.get(m.get('status'), ('','',''))[2]}">
                            {MOD_STATUS_ICONS.get(m.get('status'), ('','',''))[0]} {m.get('name')}
                        </span>"""
                        for m in modules[:6]
                    ])
                    st.markdown(mod_chips_html, unsafe_allow_html=True)
                else:
                    st.caption("No specific modules extracted.")

            with st.expander(f"📦 View Parcel-Delivery Timeline & Details ({len(timeline)} updates)"):
                tcol1, tcol2 = st.columns(2)
                with tcol1:
                    st.markdown("#### 📋 Module Checklist Details")
                    if modules:
                        for m in modules:
                            icon, lbl, _ = MOD_STATUS_ICONS.get(m.get("status"), ("•", "Pending", ""))
                            st.markdown(f"- **{icon} {m.get('name')}** — `{lbl}`")
                            if m.get("notes"):
                                st.caption(f"  *{m.get('notes')}*")
                    else:
                        st.caption("No modules recorded.")

                with tcol2:
                    st.markdown("#### 🚚 Status Delivery Timeline")
                    if timeline:
                        for ev in timeline:
                            actor_tag = "👤 Client" if ev.get("actor") == "client" else "🏢 Software Team"
                            st.markdown(f"""
                            <div class="timeline-box">
                                <div style="font-weight:700; font-size:0.9rem;">{ev.get('event')}</div>
                                <div style="font-size:0.75rem; color:#94a3b8; margin-top:2px;">
                                    📅 {ev.get('date', 'Recent')} · {actor_tag}
                                </div>
                                {f'<div style="font-size:0.8rem; margin-top:4px;">{ev.get("detail")}</div>' if ev.get("detail") else ''}
                            </div>
                            """, unsafe_allow_html=True)
                    else:
                        st.caption("No timeline history.")
            
            st.divider()


# ── AI ASSISTANT CHAT TAB ─────────────────────────────────────
st.subheader("💬 Ask AI About Clients & Modules")

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

for msg in st.session_state.chat_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_q = st.chat_input("Ask a question (e.g. Which clients confirmed their modules? Who handles Amazon?)")

if user_q:
    st.session_state.chat_history.append({"role": "user", "content": user_q})
    with st.chat_message("user"):
        st.markdown(user_q)

    email_snippets = "\n".join([
        f"[{e.get('brand')}] Handled by: {e.get('handled_by')} | Date: {e.get('date')} | Subject: {e.get('subject')}\n{e.get('body','')[:500]}"
        for e in st.session_state.emails[:25]
    ])
    client_snippets = json.dumps(st.session_state.clients[:10], indent=2)

    prompt = f"""
    You are an intelligent project management assistant for client software onboarding.
    Answer the user's question accurately using the client and email data below:

    CLIENT ONBOARDING DATA:
    {client_snippets}

    RECENT EMAILS:
    {email_snippets}

    USER QUESTION:
    {user_q}
    """
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                r = gemini.models.generate_content(
                    model=MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(max_output_tokens=1000)
                )
                answer = r.text or "No answer returned."
            except Exception as e:
                answer = f"AI Error: {e}"
            st.markdown(answer)
            st.session_state.chat_history.append({"role": "assistant", "content": answer})
