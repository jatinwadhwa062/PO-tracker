"""
app.py — Brillare PO Delivery Tracker (shared, multi-user, logged)
--------------------------------------------------------------------------------
LOGIN: a dedicated login page - enter your name + password. The password
determines your role:
    admin_password  -> full access: upload data, Mark Delayed/On Time,
                        Mark Received, Bin/Restore, see the Activity Log
    viewer_password -> can see everything and Mark Delayed/On Time, but
                        CANNOT Mark Received or Bin - those are admin-only
If neither secret is set, everyone gets admin access (no login required) -
same as the very first version, for a clean local test run.

ACTIVITY LOG: every login and every action (mark delayed, mark received,
bin, restore, upload, date/reason changes) is recorded with who did it and
when. Only the admin can see the log - it's a new tab that simply doesn't
exist for anyone else.

SHARED FILES on the server (not browser memory):
    shared_data/tracker.xlsx     - the uploaded PO_Delivery_Tracker_Final.xlsx
    shared_data/state.json       - every row's status/received/binned/date/reason
    shared_data/activity_log.json - append-only log of every action
"""

import io
import os
import json
import datetime as dt
import streamlit as st
import pandas as pd

st.set_page_config(page_title="PO Delivery Tracker", layout="wide")

SHARED_DIR = "shared_data"
SHARED_XLSX_PATH = os.path.join(SHARED_DIR, "tracker.xlsx")
SHARED_STATE_PATH = os.path.join(SHARED_DIR, "state.json")
ACTIVITY_LOG_PATH = os.path.join(SHARED_DIR, "activity_log.json")
os.makedirs(SHARED_DIR, exist_ok=True)

REASON_OPTIONS = ["RM Connectivity", "PM Connectivity", "Others"]

STOCK_BADGE = {
    "Stressed": ("🔴", "#FFC7CE", "#9C0006"),
    "Watch": ("🟡", "#FFEB9C", "#9C6500"),
    "Relaxed": ("🟢", "#C6EFCE", "#006100"),
    "Low Movement": ("⚪", "#D9D9D9", "#595959"),
    "No DRR Data": ("⚪", "#E9E9E9", "#757575"),
}


# ---------------------------------------------------------------------------
# SHARED STATE HELPERS
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


def load_shared_state():
    return load_json(SHARED_STATE_PATH, {})


def save_shared_state(state):
    save_json(SHARED_STATE_PATH, state)


def row_default(rid, state, pipeline_delayed):
    return state.setdefault(rid, {
        "status": "Delayed" if pipeline_delayed else "On Time",
        "received": False,
        "binned": False,
        "revised_date": None,
        "reason": None,
    })


def log_action(action):
    user = st.session_state.get("user_name", "Unknown")
    log = load_json(ACTIVITY_LOG_PATH, [])
    log.append({
        "time": dt.datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "user": user,
        "action": action,
    })
    log = log[-1000:]  # cap growth
    save_json(ACTIVITY_LOG_PATH, log)


# ---------------------------------------------------------------------------
# LOGIN PAGE
# ---------------------------------------------------------------------------
def get_secret(name):
    try:
        return st.secrets.get(name)
    except Exception:
        return None


def login_page():
    admin_pw = get_secret("admin_password")
    viewer_pw = get_secret("viewer_password")
    legacy_pw = get_secret("app_password")

    if not admin_pw and not viewer_pw and not legacy_pw:
        st.session_state["role"] = "admin"
        st.session_state["user_name"] = "Local User"
        return True

    if st.session_state.get("role"):
        return True

    st.markdown("<br><br>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1, 1.2, 1])
    with c2:
        st.title("📦 PO Delivery Tracker")
        st.subheader("Login")
        name = st.text_input("Your Name")
        pw = st.text_input("Password", type="password")
        if st.button("Login", use_container_width=True):
            if not name.strip():
                st.error("Please enter your name.")
            elif admin_pw and pw == admin_pw:
                st.session_state["role"] = "admin"
                st.session_state["user_name"] = name.strip()
                log_action("Logged in (admin)")
                st.rerun()
            elif legacy_pw and pw == legacy_pw:
                st.session_state["role"] = "admin"
                st.session_state["user_name"] = name.strip()
                log_action("Logged in (admin)")
                st.rerun()
            elif viewer_pw and pw == viewer_pw:
                st.session_state["role"] = "editor"
                st.session_state["user_name"] = name.strip()
                log_action("Logged in (editor)")
                st.rerun()
            else:
                st.error("Wrong password.")
    return False


if not login_page():
    st.stop()

is_admin = st.session_state["role"] == "admin"
current_user = st.session_state["user_name"]


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
# LOAD / UPLOAD DATA
# ---------------------------------------------------------------------------
st.title("📦 PO Delivery Tracker")
st.caption(f"Logged in as **{current_user}** ({'Admin' if is_admin else 'Editor'})")

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

mtime = pd.Timestamp.fromtimestamp(os.path.getmtime(SHARED_XLSX_PATH))
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

shared_state = load_shared_state()
for _, row in po_master.iterrows():
    row_default(row["_rid"], shared_state, row.get("Delivery Status") == "Delayed")
save_shared_state(shared_state)

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

        # Editors only get the status toggle (+ date/reason if Delayed).
        # Received and Bin are admin-only.
        n_buttons = 1 + (2 if is_admin else 0)
        widths = ([1] * n_buttons) + ([1.3, 1.5] if show_delay_controls else [])
        cols = st.columns(widths)
        ci = 0

        if rstate["status"] == "On Time":
            if cols[ci].button("🔴 Mark Delayed", key=f"toggle_{rid}"):
                shared_state[rid]["status"] = "Delayed"
                save_shared_state(shared_state)
                log_action(f"Marked {po_no} / {item_code} as Delayed")
                st.rerun()
        else:
            if cols[ci].button("🟢 Mark On Time", key=f"toggle_{rid}"):
                shared_state[rid]["status"] = "On Time"
                save_shared_state(shared_state)
                log_action(f"Marked {po_no} / {item_code} as On Time")
                st.rerun()
        ci += 1

        if is_admin:
            recv_label = "✅ Received" if rstate["received"] else "📥 Mark Received"
            if cols[ci].button(recv_label, key=f"recv_{rid}"):
                shared_state[rid]["received"] = not rstate["received"]
                save_shared_state(shared_state)
                log_action(f"{'Marked' if not rstate['received'] else 'Unmarked'} {po_no} / {item_code} as Received")
                st.rerun()
            ci += 1

            if cols[ci].button("🗑️ Bin", key=f"bin_{rid}"):
                shared_state[rid]["binned"] = True
                save_shared_state(shared_state)
                log_action(f"Binned {po_no} / {item_code}")
                st.rerun()
            ci += 1

        if show_delay_controls:
            existing_date = pd.to_datetime(rstate["revised_date"]).date() if rstate["revised_date"] else None
            new_date = cols[ci].date_input("Revised Date", value=existing_date, key=f"date_{rid}",
                                            label_visibility="collapsed")
            if new_date != existing_date:
                shared_state[rid]["revised_date"] = new_date.isoformat() if new_date else None
                save_shared_state(shared_state)
                log_action(f"Set revised date for {po_no} / {item_code} to {new_date}")
            ci += 1

            reason_index = REASON_OPTIONS.index(rstate["reason"]) if rstate["reason"] in REASON_OPTIONS else None
            new_reason = cols[ci].selectbox("Reason", REASON_OPTIONS, index=reason_index, key=f"reason_{rid}",
                                             placeholder="Reason", label_visibility="collapsed")
            if new_reason != rstate["reason"]:
                shared_state[rid]["reason"] = new_reason
                save_shared_state(shared_state)
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
    tab_labels += ["📜 Activity Log"]

tabs = st.tabs(tab_labels)
t_ontime, t_delayed = tabs[0], tabs[1]
idx = 2
if is_admin:
    t_bin = tabs[idx]; idx += 1
t_stock = tabs[idx]; idx += 1
if is_admin:
    t_log = tabs[idx]

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

    st.divider()
    export_rows = []
    for _, row in delayed_rows.iterrows():
        rstate = shared_state[row["_rid"]]
        if rstate["revised_date"]:
            export_rows.append({
                "Purchase Order": row["Purchase Order"],
                "Item Code": row["Item Code"],
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
                    save_shared_state(shared_state)
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
            log_df = pd.DataFrame(log_entries[::-1])  # most recent first
            st.dataframe(log_df, use_container_width=True, hide_index=True)
