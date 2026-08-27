"""
app.py — Brillare PO Delivery Tracker (Firestore-backed, with email triggers)
--------------------------------------------------------------------------------
PERSISTENT STORAGE: Firestore, not local disk or Sheets - survives Streamlit
Cloud sleeping/waking. Requires one secret:
    firebase_service_account = { ...service account JSON... }

FIRESTORE COLLECTIONS:
    users/{username}        - {salt, hash, role}
    po_state/{rid}           - snapshot (PO/SKU/stock info, refreshed every
                                upload) + interactive marks (status/received/
                                binned/revised_date/reason/reminders_sent),
                                preserved across uploads
    activity_log/{auto_id}   - every action, admin-only to view
    mail/{auto_id}           - documents here are picked up and actually sent
                                by the Firebase "Trigger Email" extension

EMAIL TRIGGERS (fired immediately from this app, not scheduled):
    1. Sagar sets/changes a revised delivery date -> emails you (admin_email)
       with SKU, promised date, Current SOH, Total DRR, Days of Cover, Status
    2. Sagar picks a delay reason -> routes to Madri (PM Connectivity) or
       Pratham (RM Connectivity). "Others" sends nothing - handled verbally.
    The 15/7/1-day reminder emails to Sagar are a SEPARATE, genuinely
    scheduled job - see functions/main.py, since Streamlit itself has no
    background process to run that on a clock.

ROLES: admin (full control) / editor (mark Delayed/On Time only) /
       viewer (read-only, zero interactive elements)
"""

import io
import json
import hashlib
import datetime as dt
from zoneinfo import ZoneInfo
import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore

st.set_page_config(page_title="PO Delivery Tracker", layout="wide")

# Light polish via standard HTML elements only (buttons, hr, headings) -
# deliberately NOT targeting Streamlit's internal data-testid/class names,
# since those change across versions and would silently stop working on a
# future Streamlit update, same category of issue we already hit twice.
st.markdown("""
<style>
    div.stButton > button {
        border-radius: 8px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        transition: box-shadow 0.15s ease, transform 0.05s ease;
    }
    div.stButton > button:hover {
        box-shadow: 0 3px 8px rgba(0,0,0,0.14);
    }
    div.stButton > button:active {
        transform: scale(0.98);
    }
    hr { margin: 0.6rem 0; opacity: 0.4; }
    h3 { margin-top: 0.2rem; margin-bottom: 0.6rem; }
</style>
""", unsafe_allow_html=True)

IST = ZoneInfo("Asia/Kolkata")
REASON_OPTIONS = ["RM Connectivity", "PM Connectivity", "Others"]

STOCK_BADGE = {
    "Stressed": ("🔴", "#FFC7CE", "#9C0006"),
    "Watch": ("🟡", "#FFEB9C", "#9C6500"),
    "Relaxed": ("🟢", "#C6EFCE", "#006100"),
    "Low Movement": ("⚪", "#D9D9D9", "#595959"),
    "No DRR Data": ("⚪", "#E9E9E9", "#757575"),
}


def sanitize_doc_id(s):
    """Firestore document IDs can't contain '/' (parsed as a path separator)
    or be exactly '.' or '..'. Real PO numbers in this data (e.g.
    'POPUR2627/046') contain '/', so this must be applied to every value
    used when building a document ID."""
    s = str(s).replace("/", "_").replace("\\", "_")
    if s in (".", ".."):
        s = f"_{s}_"
    return s


def get_secret(name, default=None):
    try:
        val = st.secrets.get(name)
        return val if val is not None else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# FIRESTORE CLIENT
# ---------------------------------------------------------------------------
@st.cache_resource
def get_db():
    if not firebase_admin._apps:
        cred_dict = dict(st.secrets["firebase_service_account"])
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    return firestore.client()


def firestore_call(fn, *args, **kwargs):
    """Wraps every Firestore call so a misconfigured/unreachable backend
    fails with a clear message instead of a cryptic crash."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        st.error(f"Couldn't reach Firestore ({e}). Check firebase_service_account in Secrets.")
        st.stop()


# ---------------------------------------------------------------------------
# USERS
# ---------------------------------------------------------------------------
def load_users():
    db = get_db()
    docs = firestore_call(lambda: list(db.collection("users").stream()))
    return {d.id: d.to_dict() for d in docs}


def save_user(username, data):
    db = get_db()
    firestore_call(db.collection("users").document(sanitize_doc_id(username.lower().strip())).set, data)


def delete_user(username):
    db = get_db()
    firestore_call(db.collection("users").document(sanitize_doc_id(username.lower().strip())).delete)


def hash_password(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 100_000).hex()


def create_user_entry(password, role):
    import os
    salt = os.urandom(16).hex()
    return {"salt": salt, "hash": hash_password(password, salt), "role": role}


def verify_user(users, username, password):
    u = users.get(sanitize_doc_id(username.lower().strip()))
    if not u:
        return None
    if hash_password(password, u["salt"]) == u["hash"]:
        return u["role"]
    return None


# ---------------------------------------------------------------------------
# ACTIVITY LOG
# ---------------------------------------------------------------------------
def log_action(action):
    db = get_db()
    user = st.session_state.get("username", "Unknown")
    firestore_call(db.collection("activity_log").add, {
        "time": dt.datetime.now(IST).strftime("%d %b %Y, %I:%M %p"),
        "timestamp": firestore.SERVER_TIMESTAMP,
        "user": user,
        "action": action,
    })


def load_log(limit=300):
    db = get_db()
    docs = firestore_call(lambda: list(
        db.collection("activity_log").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit).stream()
    ))
    return [d.to_dict() for d in docs]


# ---------------------------------------------------------------------------
# EMAIL (writes to the 'mail' collection - the Firebase Trigger Email
# extension watches this and actually sends the message)
# ---------------------------------------------------------------------------
def send_mail(to_email, subject, html_body):
    """Sends email directly via Gmail SMTP - no Firebase Extension needed
    (Firebase Extensions are being retired March 2027, so avoiding that
    dependency entirely rather than building on something already sunset)."""
    if not to_email:
        return
    gmail_user = get_secret("gmail_user")
    gmail_app_password = get_secret("gmail_app_password")
    if not gmail_user or not gmail_app_password:
        st.warning(f"Email not sent to {to_email} - gmail_user/gmail_app_password not set in Secrets.")
        return

    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_email if isinstance(to_email, str) else ", ".join(to_email)
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_app_password)
            server.sendmail(gmail_user, [to_email] if isinstance(to_email, str) else to_email, msg.as_string())
    except Exception as e:
        st.warning(f"Couldn't send email to {to_email}: {e}")


def round_num(n):
    """Rounds to a whole number for display - no decimals on SOH/DRR anywhere,
    including inside emails, matching how they're shown in the app."""
    if n is None or (isinstance(n, float) and pd.isna(n)):
        return "-"
    return f"{round(float(n)):,}"


def build_batch_table_html(title, items, intro=""):
    rows_html = "".join(
        f"<tr><td>{i['po_no']}</td><td>{i['item_code']}</td><td>{i.get('sku_name', '')}</td>"
        f"<td>{i.get('status', '')}</td>"
        f"<td>{i.get('revised_date') or '-'}</td><td>{i.get('reason') or '-'}</td>"
        f"<td>{round_num(i.get('current_soh'))}</td><td>{round_num(i.get('total_drr'))}</td>"
        f"<td>{i.get('stock_status', '')}</td></tr>"
        for i in items
    )
    return f"""
    <h3>{title}</h3>
    <p>{intro}</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">
      <tr><th>PO No.</th><th>Item Code</th><th>SKU Name</th><th>Status</th>
          <th>Promised Date</th><th>Reason</th><th>Current SOH</th><th>Total DRR</th><th>Stock Status</th></tr>
      {rows_html}
    </table>
    """


def submit_updates(dirty_items, admin_email, madri_email, pratham_email, submitted_by):
    """Sends exactly 3 batch emails (not one per row): full summary to admin,
    RM Connectivity subset to Pratham, PM Connectivity subset to Madri."""
    if not dirty_items:
        return 0

    admin_html = build_batch_table_html(
        f"Delivery updates submitted by {submitted_by}", dirty_items,
        f"{len(dirty_items)} PO line(s) were updated in this submission."
    )
    send_mail(admin_email, f"Delivery updates from {submitted_by} - {len(dirty_items)} line(s)", admin_html)

    rm_items = [i for i in dirty_items if i.get("reason") == "RM Connectivity"]
    if rm_items:
        html = build_batch_table_html("RM Connectivity delays", rm_items, "Please follow up on these.")
        send_mail(pratham_email, f"RM Connectivity delay - {len(rm_items)} line(s)", html)

    pm_items = [i for i in dirty_items if i.get("reason") == "PM Connectivity"]
    if pm_items:
        html = build_batch_table_html("PM Connectivity delays", pm_items, "Please follow up on these.")
        send_mail(madri_email, f"PM Connectivity delay - {len(pm_items)} line(s)", html)

    return len(dirty_items)


# ---------------------------------------------------------------------------
# PO STATE (snapshot + interactive marks, one Firestore doc per PO+SKU line)
# ---------------------------------------------------------------------------
def load_state():
    db = get_db()
    docs = firestore_call(lambda: list(db.collection("po_state").stream()))
    return {d.id: d.to_dict() for d in docs}


def save_row(rid, data):
    db = get_db()
    firestore_call(db.collection("po_state").document(rid).set, data, merge=True)


def delete_row(rid):
    db = get_db()
    firestore_call(db.collection("po_state").document(rid).delete)


def find_and_remove_ghost_duplicates():
    """One-time cleanup for the position-based-ID bug: finds groups of
    documents that represent the same real PO+SKU+RequiredBy+Qty line, and
    where one copy is binned but another isn't (the binned one is your real
    action; the non-binned one is a stale ghost created before this fix),
    deletes only the ghost(s), keeps the binned record."""
    all_state = load_state()
    groups = {}
    for rid, data in all_state.items():
        key = (data.get("po_no"), data.get("item_code"), data.get("required_by"), data.get("qty"))
        groups.setdefault(key, []).append((rid, data))

    to_delete = []
    for key, entries in groups.items():
        if len(entries) <= 1:
            continue
        binned_entries = [e for e in entries if e[1].get("binned")]
        non_binned_entries = [e for e in entries if not e[1].get("binned")]
        if binned_entries and non_binned_entries:
            to_delete.extend([rid for rid, _ in non_binned_entries])

    for rid in to_delete:
        delete_row(rid)
    return to_delete


def sync_snapshot(rid, snapshot, existing):
    """Refreshes the pipeline-computed fields (SKU/PO/stock info) on upload,
    while preserving Sagar's interactive marks if this row already existed."""
    data = dict(snapshot)
    if existing:
        for k in ("status", "received", "binned", "revised_date", "reason", "reminders_sent"):
            if k in existing:
                data[k] = existing[k]
    else:
        data.setdefault("status", "Delayed" if snapshot.get("pipeline_delayed") else "On Time")
        data.setdefault("received", False)
        data.setdefault("binned", False)
        data.setdefault("revised_date", None)
        data.setdefault("reason", None)
        data.setdefault("reminders_sent", [])
    save_row(rid, data)
    return data


# ---------------------------------------------------------------------------
# LOGIN / FIRST-TIME SETUP
# ---------------------------------------------------------------------------
def auth_gate():
    users = load_users()

    if st.session_state.get("username"):
        return True

    c1, c2, c3 = st.columns([1, 1.2, 1])
    with c2:
        st.markdown("<br>", unsafe_allow_html=True)
        st.title("📦 PO Delivery Tracker")

        if not users:
            st.subheader("First-time Setup")
            st.caption("No accounts exist yet. Create the first admin account to get started.")
            setup_pw_secret = get_secret("setup_password")
            setup_pw = st.text_input("Setup Password", type="password")
            new_username = st.text_input("Choose a Username")
            new_password = st.text_input("Choose a Password", type="password")
            if st.button("Create Admin Account", use_container_width=True):
                if not setup_pw_secret:
                    st.error("No setup_password configured in Secrets - add one before continuing.")
                elif setup_pw != setup_pw_secret:
                    st.error("Wrong setup password.")
                elif not new_username.strip() or not new_password:
                    st.error("Enter a username and password.")
                else:
                    save_user(new_username, create_user_entry(new_password, "admin"))
                    st.session_state["username"] = new_username.strip()
                    st.session_state["role"] = "admin"
                    log_action("Created first admin account and logged in")
                    st.rerun()
            return False

        st.subheader("Login")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.button("Login", use_container_width=True):
            role = verify_user(users, username, password)
            if role:
                st.session_state["username"] = username.strip()
                st.session_state["role"] = role
                log_action(f"Logged in ({role})")
                st.rerun()
            else:
                st.error("Wrong username or password.")
    return False


if not auth_gate():
    st.stop()

is_admin = st.session_state["role"] == "admin"
can_edit = st.session_state["role"] in ("admin", "editor")
current_user = st.session_state["username"]

ADMIN_EMAIL = get_secret("admin_email", "you@example.com")
MADRI_EMAIL = get_secret("madri_email", "madri@example.com")
PRATHAM_EMAIL = get_secret("pratham_email", "pratham@example.com")


def badge(text, bg, fg):
    return f'<span style="background-color:{bg};color:{fg};padding:2px 9px;border-radius:11px;font-weight:600;font-size:0.82em;">{text}</span>'


def fmt_date(d):
    if pd.isna(d):
        return "-"
    return pd.to_datetime(d).strftime("%d %b %Y")


def fmt_num(n):
    if pd.isna(n):
        return "-"
    n = float(n)
    return f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"


# ---------------------------------------------------------------------------
# HEADER / LOGOUT
# ---------------------------------------------------------------------------
hcol1, hcol2 = st.columns([5, 1])
hcol1.title("📦 PO Delivery Tracker")
hcol1.caption(f"Logged in as **{current_user}** ({st.session_state['role'].capitalize()})")
if hcol2.button("Log out"):
    log_action("Logged out")
    st.session_state.pop("username", None)
    st.session_state.pop("role", None)
    st.rerun()

# ---------------------------------------------------------------------------
# LOAD / UPLOAD DATA
# ---------------------------------------------------------------------------
if is_admin:
    uploaded = st.file_uploader("Upload PO_Delivery_Tracker_Final.xlsx to update everyone's view", type=["xlsx"])

    with st.expander("🧹 Clean up duplicate entries (one-time, run if a binned item is also showing in On Time/Delayed)"):
        st.caption("Fixes stale duplicates left over from before an ID-stability bug was fixed. Safe to run anytime - only removes a duplicate when one copy is already binned.")
        if st.button("Run cleanup now"):
            removed = find_and_remove_ghost_duplicates()
            if removed:
                log_action(f"Cleanup: removed {len(removed)} ghost duplicate(s)")
                st.success(f"Removed {len(removed)} stale duplicate(s).")
            else:
                st.info("No duplicates found - nothing to clean up.")
            st.rerun()
    if uploaded is not None:
        try:
            po_master_raw = pd.read_excel(uploaded, sheet_name="PO Master")
            stock_health_raw = pd.read_excel(uploaded, sheet_name="SKU Stock Health")
        except Exception as e:
            st.error(f"Couldn't read this file - is it the right output from run_stock_tracker.py? ({e})")
            st.stop()

        po_master_raw = po_master_raw.dropna(subset=["Item Code"]) if "Item Code" in po_master_raw.columns else po_master_raw
        stock_health_raw = stock_health_raw.dropna(subset=["Item Code"]) if "Item Code" in stock_health_raw.columns else stock_health_raw
        sh_lookup = stock_health_raw.set_index("Item Code").to_dict("index") if "Item Code" in stock_health_raw.columns else {}

        po_master_raw = po_master_raw.reset_index(drop=True)
        existing_state = load_state()
        synced = 0
        key_counts = {}
        for i, row in po_master_raw.iterrows():
            # Stable ID built from the PO's own business fields, NOT row position -
            # position shifts on every upload (rows get removed as they're received,
            # new ones added, sort order changes), which was silently creating a
            # brand-new "On Time" document for the same real PO every time instead
            # of finding and preserving the one already binned/marked. The
            # occurrence counter only disambiguates genuine duplicate lines
            # (same PO+Item+Date+Qty appearing more than once in ONE upload).
            base_key = (
                f"{sanitize_doc_id(row['Purchase Order'])}|{sanitize_doc_id(row['Item Code'])}|"
                f"{sanitize_doc_id(row['Required By'].date()) if pd.notna(row.get('Required By')) else 'none'}|"
                f"{sanitize_doc_id(row.get('Qty', 0))}"
            )
            key_counts[base_key] = key_counts.get(base_key, 0) + 1
            occurrence = key_counts[base_key]
            rid = base_key if occurrence == 1 else f"{base_key}|{occurrence}"

            sh = sh_lookup.get(row["Item Code"], {})
            snapshot = {
                "po_no": str(row["Purchase Order"]), "item_code": str(row["Item Code"]),
                "sku_name": str(row.get("SKU Name", "")), "supplier": str(row.get("Supplier", "")),
                "qty": float(row.get("Qty", 0)), "received_qty": float(row.get("Received Qty", 0)),
                "pending_qty": float(row.get("Pending Qty", 0)),
                "required_by": str(row["Required By"].date()) if pd.notna(row.get("Required By")) else None,
                "total_drr": float(sh.get("Total DRR", 0)) if pd.notna(sh.get("Total DRR")) else 0,
                "current_soh": float(sh.get("SOH Online", 0) or 0) + float(sh.get("SOH Offline", 0) or 0),
                "days_of_cover": float(sh["Days of Cover"]) if pd.notna(sh.get("Days of Cover")) else None,
                "stock_status": sh.get("Stock Status", "No DRR Data"),
                "pipeline_delayed": row.get("Delivery Status") == "Delayed",
            }
            sync_snapshot(rid, snapshot, existing_state.get(rid))
            synced += 1
        log_action(f"Uploaded new tracker data ({synced} PO lines synced)")
        st.success(f"Uploaded - synced {synced} PO lines. This is now what everyone sees.")
        st.rerun()

shared_state = load_state()
if not shared_state:
    st.info("No data uploaded yet." if is_admin else "No data uploaded yet - ask your admin to upload today's file.")
    st.stop()

po_master = pd.DataFrame([{**v, "_rid": k} for k, v in shared_state.items()])
po_master["Required By"] = pd.to_datetime(po_master["required_by"], errors="coerce")

# ---------------------------------------------------------------------------
# VENDOR + SKU SEARCH FILTER BAR
# ---------------------------------------------------------------------------
VENDOR_MAP = {
    "Cletza": "CLETZA LIFESCIENCE LLP",
    "Arovea": "AROVEA FORMULATIONS PVT. LTD.",
    "Zymo": "ZYMO COSMETICS",
}

st.divider()
fcol1, fcol2 = st.columns([1.3, 2])
vendor_choice = fcol1.radio("3P Vendor", ["All 3Ps"] + list(VENDOR_MAP.keys()), horizontal=True)
sku_search = fcol2.text_input("🔍 Search SKU Code", placeholder="e.g. RMSH200")

po_filtered = po_master.copy()
if vendor_choice != "All 3Ps":
    po_filtered = po_filtered[po_filtered["supplier"].astype(str).str.strip() == VENDOR_MAP[vendor_choice]]
if sku_search:
    mask = po_filtered["item_code"].astype(str).str.contains(sku_search, case=False, na=False)
    mask |= po_filtered["sku_name"].astype(str).str.contains(sku_search, case=False, na=False)
    po_filtered = po_filtered[mask]
    if len(po_filtered):
        matched_vendors = sorted(po_filtered["supplier"].dropna().unique())
        if matched_vendors:
            st.caption(f"Found in: {', '.join(matched_vendors)}")
    else:
        st.caption("No open POs match that SKU.")

po_master = po_filtered


def render_row_body(row, show_delay_controls):
    """Everything shown inside an expanded tile. Stacked vertically, not
    side-by-side - tiles sit inside a narrow grid column now, so nested
    horizontal columns would get cramped."""
    rid = row["_rid"]
    rstate = shared_state[rid]
    po_no, item_code = row["po_no"], row["item_code"]

    st.caption(f'{po_no} &nbsp;·&nbsp; Required By: **{fmt_date(row["Required By"])}**')
    st.markdown(
        f'Qty: **{fmt_num(row["qty"])}** &nbsp;·&nbsp; Received: **{fmt_num(row["received_qty"])}** '
        f'&nbsp;·&nbsp; {badge(row["supplier"], "#E8E8E8", "#333333") if row.get("supplier") else ""}',
        unsafe_allow_html=True,
    )

    if not can_edit:
        if show_delay_controls:
            rd = rstate.get("revised_date")
            rd_text = pd.to_datetime(rd).strftime("%d %b %Y") if rd else "not set yet"
            st.caption(f"Revised Delivery Date: **{rd_text}** &nbsp;·&nbsp; Reason: **{rstate.get('reason') or '-'}**")
        return

    if rstate["status"] == "On Time":
        if st.button("🔴 Mark Delayed", key=f"toggle_{rid}", use_container_width=True):
            shared_state[rid]["status"] = "Delayed"
            save_row(rid, {"status": "Delayed"})
            log_action(f"Marked {po_no} / {item_code} as Delayed")
            st.session_state.setdefault("dirty_rids", set()).add(rid)
            st.rerun()
    else:
        if st.button("🟢 Mark On Time", key=f"toggle_{rid}", use_container_width=True):
            shared_state[rid]["status"] = "On Time"
            save_row(rid, {"status": "On Time"})
            log_action(f"Marked {po_no} / {item_code} as On Time")
            st.session_state.setdefault("dirty_rids", set()).add(rid)
            st.rerun()

    if is_admin:
        acol1, acol2 = st.columns(2)
        recv_label = "✅ Received" if rstate["received"] else "📥 Received"
        if acol1.button(recv_label, key=f"recv_{rid}", use_container_width=True):
            new_val = not rstate["received"]
            shared_state[rid]["received"] = new_val
            save_row(rid, {"received": new_val})
            log_action(f"{'Marked' if new_val else 'Unmarked'} {po_no} / {item_code} as Received")
            st.rerun()
        if acol2.button("🗑️ Bin", key=f"bin_{rid}", use_container_width=True):
            shared_state[rid]["binned"] = True
            save_row(rid, {"binned": True})
            log_action(f"Binned {po_no} / {item_code}")
            st.rerun()

    if show_delay_controls:
        existing_date = pd.to_datetime(rstate["revised_date"]).date() if rstate.get("revised_date") else None
        new_date = st.date_input("Revised Date", value=existing_date, key=f"date_{rid}")
        if new_date != existing_date:
            shared_state[rid]["revised_date"] = new_date.isoformat() if new_date else None
            shared_state[rid]["reminders_sent"] = []
            save_row(rid, {"revised_date": new_date.isoformat() if new_date else None, "reminders_sent": []})
            log_action(f"Set revised date for {po_no} / {item_code} to {new_date}")
            if new_date:
                st.session_state.setdefault("dirty_rids", set()).add(rid)

        reason_index = REASON_OPTIONS.index(rstate["reason"]) if rstate.get("reason") in REASON_OPTIONS else None
        new_reason = st.selectbox("Reason", REASON_OPTIONS, index=reason_index, key=f"reason_{rid}",
                                   placeholder="Reason")
        if new_reason != rstate.get("reason"):
            shared_state[rid]["reason"] = new_reason
            save_row(rid, {"reason": new_reason})
            log_action(f"Set delay reason for {po_no} / {item_code} to {new_reason}")
            st.session_state.setdefault("dirty_rids", set()).add(rid)

        if not shared_state[rid].get("revised_date"):
            st.warning("⚠️ Pick a revised delivery date.", icon="⚠️")


def shorten(text, n=40):
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def build_list_df(df):
    """The compact, scannable master list - one real row per SKU, not a tile.
    Renders inside a fixed-height scrolling table, so the PAGE doesn't grow
    with row count - only the small table area scrolls."""
    out = df.copy()
    out["Status"] = out["_rid"].map(lambda r: "🟢 On Time" if shared_state[r]["status"] == "On Time" else "🔴 Delayed")
    out["SKU"] = out["item_code"]
    out["Product"] = out["sku_name"].apply(shorten)
    out["Pending Qty"] = out["pending_qty"].apply(lambda n: round_num(n))
    out["Required By"] = out["Required By"].apply(fmt_date)
    out["Supplier"] = out["supplier"]
    return out[["Status", "SKU", "Product", "Pending Qty", "Required By", "Supplier", "_rid"]]


def render_detail_panel(row):
    """Everything for the ONE currently-selected SKU - full info + actions.
    At most one of these renders at a time, unlike the old tile grid where
    up to 58 expandable action-sets existed on the page simultaneously."""
    rid = row["_rid"]
    rstate = shared_state[rid]
    show_delay_controls = rstate["status"] == "Delayed"
    s_emoji, s_bg, s_fg = STOCK_BADGE.get(row["stock_status"], ("⚪", "#E9E9E9", "#757575"))

    with st.container(border=True):
        st.markdown(
            f'### {row["item_code"]} · {row.get("sku_name", "")}\n'
            f'{badge(f"{s_emoji} {row["stock_status"]}", s_bg, s_fg)} &nbsp; '
            f'{badge(row["supplier"], "#E8E8E8", "#333333") if row.get("supplier") else ""}',
            unsafe_allow_html=True,
        )
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Pending Qty", round_num(row["pending_qty"]))
        m2.metric("Received", round_num(row["received_qty"]))
        m3.metric("Ordered Qty", round_num(row["qty"]))
        m4.metric("Required By", fmt_date(row["Required By"]))
        st.caption(f'PO: {row["po_no"]}')

        render_row_body(row, show_delay_controls)


def render_master_detail(df, list_key):
    if len(df) == 0:
        st.success("Nothing here.")
        return

    list_df = build_list_df(df)
    st.caption("Click any row to view and act on it below.")
    event = st.dataframe(
        list_df.drop(columns=["_rid"]),
        use_container_width=True,
        hide_index=True,
        height=min(38 * (len(list_df) + 1) + 3, 420),
        on_select="rerun",
        selection_mode="single-row",
        key=list_key,
    )

    selected_idx = event.selection.rows if event and event.selection else []
    if selected_idx:
        rid = list_df.iloc[selected_idx[0]]["_rid"]
        selected_row = df[df["_rid"] == rid].iloc[0]
        st.divider()
        render_detail_panel(selected_row)
    else:
        st.info("👆 Select a row above to see full details and actions.")


# ---------------------------------------------------------------------------
# TAB NAVIGATION
# ---------------------------------------------------------------------------
active = po_master[~po_master["_rid"].map(lambda r: shared_state[r]["binned"])]
on_time_rows = active[active["_rid"].map(lambda r: shared_state[r]["status"] == "On Time")]
delayed_rows = active[active["_rid"].map(lambda r: shared_state[r]["status"] == "Delayed")]
binned_rows = po_master[po_master["_rid"].map(lambda r: shared_state[r]["binned"])]

tab_labels = [f"🟢 On Time ({len(on_time_rows)})", f"🔴 Delayed ({len(delayed_rows)})"]
if is_admin:
    tab_labels += [f"🗑️ Bin ({len(binned_rows)})"]
tab_labels += ["📊 Stressed & Watch SKUs"]
if is_admin:
    tab_labels += ["📜 Activity Log", "👥 Manage Users"]

tabs = st.tabs(tab_labels)
t_ontime, t_delayed = tabs[0], tabs[1]
idx = 2
if is_admin:
    t_bin = tabs[idx]; idx += 1
t_stock = tabs[idx]; idx += 1
if is_admin:
    t_log = tabs[idx]; idx += 1
    t_users = tabs[idx]

with t_ontime:
    render_master_detail(on_time_rows, list_key="list_ontime")

with t_delayed:
    render_master_detail(delayed_rows, list_key="list_delayed")

    if can_edit:
        st.divider()
        all_dirty = st.session_state.get("dirty_rids", set())
        scol1, scol2 = st.columns([1, 3])
        if scol1.button(f"📤 Submit Updates ({len(all_dirty)})", disabled=len(all_dirty) == 0,
                         type="primary", use_container_width=True):
            dirty_items = [dict(shared_state[r], _rid=r) for r in all_dirty if r in shared_state]
            n = submit_updates(dirty_items, ADMIN_EMAIL, MADRI_EMAIL, PRATHAM_EMAIL, current_user)
            log_action(f"Submitted {n} delivery update(s) - notified admin/Pratham/Madri as applicable")
            st.session_state["dirty_rids"] = set()
            st.success(f"Submitted {n} update(s) and sent notifications.")
            st.rerun()
        if len(all_dirty) == 0:
            scol2.caption("No unsent changes right now - mark something Delayed/On Time or update a date/reason first.")
        else:
            scol2.caption(f"{len(all_dirty)} change(s) ready to submit. One email to admin, plus RM/PM routed to Pratham/Madri.")

        export_rows = []
        for _, row in delayed_rows.iterrows():
            rstate = shared_state[row["_rid"]]
            if rstate.get("revised_date"):
                export_rows.append({
                    "Purchase Order": row["po_no"], "Item Code": row["item_code"],
                    "Revised Expected Delivery Date": rstate["revised_date"],
                    "Delay Reason": rstate.get("reason") or "Others",
                })
        if export_rows:
            export_df = pd.DataFrame(export_rows)
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                export_df.to_excel(writer, sheet_name="Delay Tracker", index=False)
            st.download_button(
                f"⬇️ Download Delay_Tracker.xlsx ({len(export_df)} line{'s' if len(export_df) != 1 else ''})",
                data=buf.getvalue(), file_name="Delay_Tracker.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

if is_admin:
    with t_bin:
        if len(binned_rows) == 0:
            st.success("Bin is empty.")
        for _, row in binned_rows.iterrows():
            rid = row["_rid"]
            with st.container(border=True):
                st.markdown(f'**{row["po_no"]}** — {row["item_code"]} · {row.get("sku_name", "")}')
                if st.button("♻️ Restore", key=f"restore_{rid}"):
                    shared_state[rid]["binned"] = False
                    save_row(rid, {"binned": False})
                    log_action(f"Restored {row['po_no']} / {row['item_code']} from Bin")
                    st.rerun()

with t_stock:
    urgent = po_master[po_master["stock_status"].isin(["Stressed", "Watch"])].copy()
    if sku_search:
        pass  # already filtered above via po_master
    urgent = urgent.drop_duplicates(subset=["item_code"])
    if len(urgent) == 0:
        st.success("No Stressed or Watch SKUs right now.")
    else:
        urgent["Need Delivery By"] = urgent["days_of_cover"].apply(
            lambda d: (pd.Timestamp.today().normalize() + pd.Timedelta(days=int(d))).strftime("%d %b %Y")
            if pd.notna(d) else "-"
        )
        urgent = urgent.sort_values(["stock_status", "total_drr"], ascending=[True, False])
        urgent["_drr_display"] = urgent["total_drr"].apply(round_num)
        urgent["_soh_display"] = urgent["current_soh"].apply(round_num)
        show = urgent[["sku_name", "_drr_display", "_soh_display", "Need Delivery By", "stock_status"]].rename(
            columns={"sku_name": "SKU Name", "_drr_display": "Total DRR", "_soh_display": "Current SOH",
                     "stock_status": "Stock Status"}
        )

        def highlight(df):
            def f(row):
                bg = "#FFC7CE" if row["Stock Status"] == "Stressed" else "#FFEB9C"
                fg = "#9C0006" if row["Stock Status"] == "Stressed" else "#9C6500"
                return [f"background-color:{bg}; color:{fg}"] * len(row)
            return df.style.apply(f, axis=1)

        st.caption("'Need Delivery By' = the date stock is projected to run out at current sales pace.")
        st.dataframe(highlight(show), use_container_width=True, hide_index=True)

if is_admin:
    with t_log:
        st.subheader("Activity Log")
        st.caption("Visible to admin only.")
        log_entries = load_log()
        if not log_entries:
            st.info("No activity yet.")
        else:
            st.dataframe(pd.DataFrame(log_entries)[["time", "user", "action"]], use_container_width=True, hide_index=True)

    with t_users:
        st.subheader("Manage Users")
        users = load_users()

        st.write("**Existing accounts:**")
        if users:
            user_rows = [{"Username": u, "Role": info["role"]} for u, info in users.items()]
            st.dataframe(pd.DataFrame(user_rows), use_container_width=True, hide_index=True)
        else:
            st.info("No accounts yet.")

        st.divider()
        st.write("**Add a new account:**")
        ucol1, ucol2, ucol3, ucol4 = st.columns([1.3, 1.3, 1, 0.8])
        new_u = ucol1.text_input("Username", key="newuser_name")
        new_p = ucol2.text_input("Password", type="password", key="newuser_pw")
        new_role = ucol3.selectbox("Role", ["viewer", "editor", "admin"], key="newuser_role")
        if ucol4.button("Add", use_container_width=True):
            if not new_u.strip() or not new_p:
                st.error("Enter both a username and password.")
            elif new_u.lower().strip() in users:
                st.error("That username already exists.")
            else:
                save_user(new_u, create_user_entry(new_p, new_role))
                log_action(f"Created account for '{new_u.strip()}' ({new_role})")
                st.success(f"Account created for {new_u.strip()}.")
                st.rerun()

        st.divider()
        st.write("**Remove an account:**")
        removable = [u for u in users if u != current_user.lower()]
        if removable:
            rcol1, rcol2 = st.columns([2, 1])
            to_remove = rcol1.selectbox("Username", removable, key="remove_user_select")
            if rcol2.button("Remove", use_container_width=True):
                delete_user(to_remove)
                log_action(f"Removed account '{to_remove}'")
                st.success(f"Removed {to_remove}.")
                st.rerun()
        else:
            st.caption("No other accounts to remove.")
