import os, json
from pathlib import Path
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()

# Page Configuration
st.set_page_config(
    page_title="MailBrain — Client Tracker",
    page_icon="📬",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Hide all Streamlit default chrome so the custom UI takes 100% full screen
st.markdown("""
<style>
    #MainMenu {visibility: hidden;}
    header[data-testid="stHeader"] {display: none !important;}
    footer {visibility: hidden;}
    [data-testid="stSidebar"] {display: none !important;}
    .stAppDeployButton {display: none !important;}
    div[data-testid="stToolbar"] {display: none !important;}
    .block-container {
        padding: 0 !important;
        margin: 0 !important;
        max-width: 100% !important;
        height: 100vh !important;
        overflow: hidden !important;
    }
    iframe {
        position: fixed;
        top: 0;
        left: 0;
        width: 100vw !important;
        height: 100vh !important;
        border: none !important;
        z-index: 999999;
    }
</style>
""", unsafe_allow_html=True)

BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "static" / "index.html"
CLIENTS_CACHE_FILE = BASE_DIR / "clients_cache.json"

# Resolve Gemini API Key (Secrets > .env > default)
def get_secret(key, default=""):
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)

GEMINI_API_KEY = get_secret("GEMINI_API_KEY", "")

# Load cached clients
clients_data = []
if CLIENTS_CACHE_FILE.exists():
    try:
        with open(CLIENTS_CACHE_FILE) as f:
            clients_data = json.load(f)
    except Exception as e:
        print("Error loading clients cache:", e)

# Read static/index.html
if not HTML_FILE.exists():
    st.error("static/index.html not found.")
    st.stop()

with open(HTML_FILE, "r", encoding="utf-8") as f:
    raw_html = f.read()

# Inject clients data and API key into the HTML so it matches 127.0.0.1:8000 100% identically
initial_script = f"""
<script>
window.__STREAMLIT_HOSTED__ = true;
window.__GEMINI_API_KEY__ = "{GEMINI_API_KEY}";
window.__EMBEDDED_CLIENTS__ = {json.dumps(clients_data)};

// Auto-populate on load so Streamlit Cloud displays all 9 clients immediately
document.addEventListener("DOMContentLoaded", function() {{
  if (window.__EMBEDDED_CLIENTS__ && window.__EMBEDDED_CLIENTS__.length > 0) {{
    window.clientsData = window.__EMBEDDED_CLIENTS__;
    const badge = document.getElementById('statusBadge');
    if (badge) {{
      badge.className = 'ok';
      badge.textContent = `${{window.clientsData.length}} clients cached`;
    }}
    if (typeof renderSidebar === 'function') renderSidebar();
    if (typeof renderRowView === 'function') renderRowView();
  }}
}});

// Override chat sendQ to query Gemini API directly if on Streamlit Cloud
const _origSendQ = window.sendQ;
window.sendQ = async function() {{
  const inp = document.getElementById('chatInput');
  const q = inp ? inp.value.trim() : '';
  if (!q) return;
  inp.value = '';
  addMsg(q, 'user');
  const thinking = addMsg('Thinking…', 'bot thinking');

  const apiKey = window.__GEMINI_API_KEY__;
  if (!apiKey) {{
    thinking.remove();
    addMsg("Please configure GEMINI_API_KEY in Streamlit Secrets.", "bot");
    return;
  }}

  const brand = (typeof selectedIndex !== 'undefined' && selectedIndex !== null && window.clientsData) ? window.clientsData[selectedIndex]?.brand_key : null;
  const clientContext = JSON.stringify((window.clientsData || []).slice(0, 10));

  const promptText = `You are a project management assistant analyzing software client onboarding status.
CLIENT DATA:
${{clientContext}}

QUERY:
${{q}} (Scoped brand: ${{brand || 'All'}});
Be concise and mention client stage, handling employee, and module status when relevant.`;

  try {{
    const url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${{apiKey}}`;
    const resp = await fetch(url, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        contents: [{{ parts: [{{ text: promptText }}] }}]
      }})
    }});
    const data = await resp.json();
    thinking.remove();
    const reply = data?.candidates?.[0]?.content?.parts?.[0]?.text || "No response returned.";
    addMsg(reply, 'bot');
  }} catch(err) {{
    thinking.remove();
    addMsg("Error: " + err.message, 'bot');
  }}
}};
</script>
"""

# Place script right before </body>
if "</body>" in raw_html:
    final_html = raw_html.replace("</body>", f"{initial_script}</body>")
else:
    final_html = raw_html + initial_script

# Render exact identical full-screen UI
components.html(final_html, height=1200, scrolling=True)
