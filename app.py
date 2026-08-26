"""
app.py — Brillare PO Delivery Tracker (real per-user accounts)
--------------------------------------------------------------------------------
AUTHENTICATION: real username + password per person, not a shared password.
Passwords are hashed (PBKDF2-HMAC-SHA256, 100,000 iterations, random salt
per user) - never stored in plain text anywhere.

FIRST-TIME SETUP: with no users yet, the app shows a one-time Setup screen
gated by a `setup_password` secret (set this in Streamlit Secrets - only
you should know it). Use it once to create your own admin account. After
that, the Setup screen never appears again - it's a normal login, and you
manage further accounts from the "Manage Users" tab inside the app.

ROLES:
    admin  -> upload data, Mark Delayed/On Time, Mark Received, Bin/Restore,
              see the Activity Log, manage user accounts
    editor -> can see everything and Mark Delayed/On Time, cannot Mark
              Received or Bin, no access to Activity Log or Manage Users

SHARED FILES on the server:
    shared_data/users.json        - username -> {salt, hash, role}
    shared_data/tracker.xlsx      - the uploaded PO_Delivery_Tracker_Final.xlsx
    shared_data/state.json        - every row's status/received/binned/date/reason
    shared_data/activity_log.json - append-only log of every action
"""

import io
import os
import json
import hashlib
import datetime as dt
from zoneinfo import ZoneInfo
import streamlit as st
import pandas as pd

st.set_page_config(page_title="PO Delivery Tracker", layout="wide")

SHARED_DIR = "shared_data"
USERS_PATH = os.path.join(SHARED_DIR, "users.json")
SHARED_XLSX_PATH = os.path.join(SHARED_DIR, "tracker.xlsx")
SHARED_STATE_PATH = os.path.join(SHARED_DIR, "state.json")
ACTIVITY_LOG_PATH = os.path.join(SHARED_DIR, "activity_log.json")
os.makedirs(SHARED_DIR, exist_ok=True)

IST = ZoneInfo("Asia/Kolkata")
REASON_OPTIONS = ["RM Connectivity", "PM Connectivity", "Others"]

STOCK_BADGE = {
    "Stressed": ("🔴", "#FFC7CE", "#9C0006"),
    "Watch": ("🟡", "#FFEB9C", "#9C6500"),
    "Relaxed": ("🟢", "#C6EFCE", "#006100"),
    "Low Movement": ("⚪", "#D9D9D9", "#595959"),
    "No DRR Data": ("⚪", "#E9E9E9", "#757575"),
}


# ---------------------------------------------------------------------------
# JSON FILE HELPERS
# ---------------------------------------------------------------------------
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# PASSWORD HASHING - PBKDF2-HMAC-SHA256, random salt per user, no plaintext
# ---------------------------------------------------------------------------
def hash_password(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 100_000).hex()


def create_user(users, username, password, role):
    salt = os.urandom(16).hex()
    users[username.lower().strip()] = {"salt": salt, "hash": hash_password(password, salt), "role": role}
    return users


def verify_user(users, username, password):
    u = users.get(username.lower().strip())
    if not u:
        return None
    if hash_password(password, u["salt"]) == u["hash"]:
        return u["role"]
    return None


def log_action(action):
    user = st.session_state.get("username", "Unknown")
    log = load_json(ACTIVITY_LOG_PATH, [])
    log.append({"time": dt.datetime.now(IST).strftime("%d %b %Y, %I:%M %p"), "user": user, "action": action})
    save_json(ACTIVITY_LOG_PATH, log[-1000:])


def get_secret(name):
    try:
        return st.secrets.get(name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LOGIN / FIRST-TIME SETUP
# ---------------------------------------------------------------------------
def auth_gate():
    users = load_json(USERS_PATH, {})

    if st.session_state.get("username"):
        return True

    c1, c2, c3 = st.columns([1, 1.2, 1])
    with c2:
        st.markdown("<br>", unsafe_allow_html=True)
        st.title("📦 PO Delivery Tracker")

        if not users:
            # ---- FIRST-TIME SETUP: create the first (admin) account ----
            st.subheader("First-time Setup")
            st.caption("No accounts exist yet. Create the first admin account to get started.")
            setup_pw_secret = get_secret("setup_password")
            setup_pw = st.text_input("Setup Password", type="password",
                                      help="Set this in Streamlit Secrets as setup_password")
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
                    create_user(users, new_username, new_password, "admin")
                    save_json(USERS_PATH, users)
                    st.session_state["username"] = new_username.strip()
                    st.session_state["role"] = "admin"
                    log_action("Created first admin account and logged in")
                    st.rerun()
            return False

        # ---- NORMAL LOGIN ----
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
can_edit = st.session_state["role"] in ("admin", "editor")  # viewer = neither
current_user = st.session_state["username"]


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
hcol1.caption(f"Logged in as **{current_user}** ({'Admin' if is_admin else 'Editor'})")
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
    if uploaded is not None:
        with open(SHARED_XLSX_PATH, "wb") as f:
            f.write(uploaded.getbuffer())
        log_action("Uploaded new tracker data")
        st.success("Uploaded - this is now what everyone sees.")

if not os.path.exists(SHARED_XLSX_PATH):
    st.info("No data uploaded yet." if is_admin else "No data uploaded yet - ask your admin to upload today's file.")
    st.stop()

mtime = pd.Timestamp.fromtimestamp(os.path.getmtime(SHARED_XLSX_PATH), tz="UTC").tz_convert(IST)
st.caption(f"Showing data uploaded: {mtime.strftime('%d %b %Y, %I:%M %p')}")

try:
    po_master = pd.read_excel(SHARED_XLSX_PATH, sheet_name="PO Master")
    stock_health = pd.read_excel(SHARED_XLSX_PATH, sheet_name="SKU Stock Health")
except Exception as e:
    st.error(f"Couldn't read the shared file - is it the right output from run_stock_tracker.py? ({e})")
    st.stop()

po_master = po_master.dropna(subset=["Item Code"]) if "Item Code" in po_master.columns else po_master
stock_health = stock_health.dropna(subset=["Item Code"]) if "Item Code" in stock_health.columns else stock_health

lookup_cols = [c for c in ["Item Code", "Stock Status"] if c in stock_health.columns]
po_master = po_master.merge(stock_health[lookup_cols], on="Item Code", how="left")
po_master["Stock Status"] = po_master["Stock Status"].fillna("No DRR Data")
po_master["Required By"] = pd.to_datetime(po_master["Required By"], errors="coerce")
po_master = po_master.reset_index(drop=True)
po_master["_rid"] = (
    po_master.index.astype(str) + "|" +
    po_master["Purchase Order"].astype(str) + "|" +
    po_master["Item Code"].astype(str)
)


def row_default(rid, state, pipeline_delayed):
    return state.setdefault(rid, {
        "status": "Delayed" if pipeline_delayed else "On Time",
        "received": False, "binned": False, "revised_date": None, "reason": None,
    })


shared_state = load_json(SHARED_STATE_PATH, {})
for _, row in po_master.iterrows():
    row_default(row["_rid"], shared_state, row.get("Delivery Status") == "Delayed")
save_json(SHARED_STATE_PATH, shared_state)

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
if vendor_choice != "All 3Ps" and "Supplier" in po_filtered.columns:
    po_filtered = po_filtered[po_filtered["Supplier"].astype(str).str.strip() == VENDOR_MAP[vendor_choice]]
if sku_search:
    mask = po_filtered["Item Code"].astype(str).str.contains(sku_search, case=False, na=False)
    if "SKU Name" in po_filtered.columns:
        mask |= po_filtered["SKU Name"].astype(str).str.contains(sku_search, case=False, na=False)
    po_filtered = po_filtered[mask]
    if len(po_filtered):
        matched_vendors = sorted(po_filtered["Supplier"].dropna().unique()) if "Supplier" in po_filtered.columns else []
        if matched_vendors:
            st.caption(f"Found in: {', '.join(matched_vendors)}")
    else:
        st.caption("No open POs match that SKU.")

po_master = po_filtered


def render_row(row, show_delay_controls):
    rid = row["_rid"]
    rstate = shared_state[rid]
    s_emoji, s_bg, s_fg = STOCK_BADGE.get(row["Stock Status"], ("⚪", "#E9E9E9", "#757575"))
    pending_type = "Full Pending" if row["Received Qty"] == 0 else "Partial"
    supplier = row.get("Supplier", "")
    po_no, item_code = row["Purchase Order"], row["Item Code"]

    with st.container(border=True):
        st.markdown(
            f'**{po_no}** — {item_code} · {row.get("SKU Name", "")} '
            f'{badge(f"{s_emoji} {row["Stock Status"]}", s_bg, s_fg)} '
            f'{badge(supplier, "#E8E8E8", "#333333") if supplier else ""}  \n'
            f'Qty: **{fmt_num(row["Qty"])}** &nbsp;·&nbsp; '
            f'Received: **{fmt_num(row["Received Qty"])}** &nbsp;·&nbsp; '
            f'Pending: **{fmt_num(row["Pending Qty"])}** ({pending_type}) &nbsp;·&nbsp; '
            f'Required By: **{fmt_date(row["Required By"])}**',
            unsafe_allow_html=True,
        )

        if not can_edit:
            # Viewer: read-only, zero buttons/widgets - just show current status as text
            if show_delay_controls:
                rd = rstate["revised_date"]
                rd_text = pd.to_datetime(rd).strftime("%d %b %Y") if rd else "not set yet"
                st.caption(f"Revised Delivery Date: **{rd_text}** &nbsp;·&nbsp; Reason: **{rstate['reason'] or '-'}**")
            return

        n_buttons = 1 + (2 if is_admin else 0)
        widths = ([1] * n_buttons) + ([1.3, 1.5] if show_delay_controls else [])
        cols = st.columns(widths)
        ci = 0

        if rstate["status"] == "On Time":
            if cols[ci].button("🔴 Mark Delayed", key=f"toggle_{rid}"):
                shared_state[rid]["status"] = "Delayed"
                save_json(SHARED_STATE_PATH, shared_state)
                log_action(f"Marked {po_no} / {item_code} as Delayed")
                st.rerun()
        else:
            if cols[ci].button("🟢 Mark On Time", key=f"toggle_{rid}"):
                shared_state[rid]["status"] = "On Time"
                save_json(SHARED_STATE_PATH, shared_state)
                log_action(f"Marked {po_no} / {item_code} as On Time")
                st.rerun()
        ci += 1

        if is_admin:
            recv_label = "✅ Received" if rstate["received"] else "📥 Mark Received"
            if cols[ci].button(recv_label, key=f"recv_{rid}"):
                shared_state[rid]["received"] = not rstate["received"]
                save_json(SHARED_STATE_PATH, shared_state)
                log_action(f"{'Marked' if not rstate['received'] else 'Unmarked'} {po_no} / {item_code} as Received")
                st.rerun()
            ci += 1

            if cols[ci].button("🗑️ Bin", key=f"bin_{rid}"):
                shared_state[rid]["binned"] = True
                save_json(SHARED_STATE_PATH, shared_state)
                log_action(f"Binned {po_no} / {item_code}")
                st.rerun()
            ci += 1

        if show_delay_controls:
            existing_date = pd.to_datetime(rstate["revised_date"]).date() if rstate["revised_date"] else None
            new_date = cols[ci].date_input("Revised Date", value=existing_date, key=f"date_{rid}",
                                            label_visibility="collapsed")
            if new_date != existing_date:
                shared_state[rid]["revised_date"] = new_date.isoformat() if new_date else None
                save_json(SHARED_STATE_PATH, shared_state)
                log_action(f"Set revised date for {po_no} / {item_code} to {new_date}")
            ci += 1

            reason_index = REASON_OPTIONS.index(rstate["reason"]) if rstate["reason"] in REASON_OPTIONS else None
            new_reason = cols[ci].selectbox("Reason", REASON_OPTIONS, index=reason_index, key=f"reason_{rid}",
                                             placeholder="Reason", label_visibility="collapsed")
            if new_reason != rstate["reason"]:
                shared_state[rid]["reason"] = new_reason
                save_json(SHARED_STATE_PATH, shared_state)
                log_action(f"Set delay reason for {po_no} / {item_code} to {new_reason}")

            if not shared_state[rid]["revised_date"]:
                st.warning("⚠️ Pick a revised delivery date.", icon="⚠️")


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
    if len(on_time_rows) == 0:
        st.success("Nothing here.")
    for _, row in on_time_rows.iterrows():
        render_row(row, show_delay_controls=False)

with t_delayed:
    if len(delayed_rows) == 0:
        st.success("Nothing here.")
    for _, row in delayed_rows.iterrows():
        render_row(row, show_delay_controls=True)

    if can_edit:
        st.divider()
        export_rows = []
        for _, row in delayed_rows.iterrows():
            rstate = shared_state[row["_rid"]]
            if rstate["revised_date"]:
                export_rows.append({
                    "Purchase Order": row["Purchase Order"], "Item Code": row["Item Code"],
                    "Revised Expected Delivery Date": rstate["revised_date"],
                    "Delay Reason": rstate["reason"] or "Others",
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
            st.caption("Drop this into your 3P Tracker folder before the next run_stock_tracker.py run.")
        else:
            st.info("Pick a date on at least one Delayed line to enable the download.")

if is_admin:
    with t_bin:
        if len(binned_rows) == 0:
            st.success("Bin is empty.")
        for _, row in binned_rows.iterrows():
            rid = row["_rid"]
            with st.container(border=True):
                st.markdown(f'**{row["Purchase Order"]}** — {row["Item Code"]} · {row.get("SKU Name", "")}')
                if st.button("♻️ Restore", key=f"restore_{rid}"):
                    shared_state[rid]["binned"] = False
                    save_json(SHARED_STATE_PATH, shared_state)
                    log_action(f"Restored {row['Purchase Order']} / {row['Item Code']} from Bin")
                    st.rerun()

with t_stock:
    need_cols = {"Stock Status", "SKU Name", "Total DRR", "SOH Online", "SOH Offline", "Days of Cover"}
    if not need_cols.issubset(stock_health.columns):
        st.warning("Some columns needed for this section aren't in the uploaded file.")
    else:
        urgent = stock_health[stock_health["Stock Status"].isin(["Stressed", "Watch"])].copy()
        if sku_search:
            mask3 = urgent["Item Code"].astype(str).str.contains(sku_search, case=False, na=False)
            if "SKU Name" in urgent.columns:
                mask3 |= urgent["SKU Name"].astype(str).str.contains(sku_search, case=False, na=False)
            urgent = urgent[mask3]
        if len(urgent) == 0:
            st.success("No Stressed or Watch SKUs right now.")
        else:
            urgent["Current SOH"] = urgent["SOH Online"].fillna(0) + urgent["SOH Offline"].fillna(0)
            urgent["Need Delivery By"] = urgent["Days of Cover"].apply(
                lambda d: (pd.Timestamp.today().normalize() + pd.Timedelta(days=int(d))).strftime("%d %b %Y")
                if pd.notna(d) else "-"
            )
            urgent = urgent.sort_values(["Stock Status", "Total DRR"], ascending=[True, False])
            show = urgent[["SKU Name", "Total DRR", "Current SOH", "Need Delivery By", "Stock Status"]]

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
        log_entries = load_json(ACTIVITY_LOG_PATH, [])
        if not log_entries:
            st.info("No activity yet.")
        else:
            log_df = pd.DataFrame(log_entries[::-1])
            st.dataframe(log_df, use_container_width=True, hide_index=True)

    with t_users:
        st.subheader("Manage Users")
        users = load_json(USERS_PATH, {})

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
                create_user(users, new_u, new_p, new_role)
                save_json(USERS_PATH, users)
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
                del users[to_remove]
                save_json(USERS_PATH, users)
                log_action(f"Removed account '{to_remove}'")
                st.success(f"Removed {to_remove}.")
                st.rerun()
        else:
            st.caption("No other accounts to remove.")
