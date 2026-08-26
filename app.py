"""
app.py — Brillare PO Delivery Tracker (shared, multi-user)
--------------------------------------------------------------------------------
Everyone who opens this app's URL sees the SAME data and the SAME marks
(Delayed/Received/Bin) - not separate per-browser copies. This works by
writing to two small files on the server instead of keeping everything in
browser memory:
    shared_data/tracker.xlsx    - the uploaded PO_Delivery_Tracker_Final.xlsx
    shared_data/state.json      - every row's On Time/Delayed/Received/Bin/
                                   revised date/reason, keyed by row ID

TWO ACCESS LEVELS (set in the app's Secrets on Streamlit Cloud):
    admin_password  = "..."   -> can upload/replace the master data file,
                                  AND mark Delayed/Received/Bin
    viewer_password = "..."   -> can only mark Delayed/Received/Bin,
                                  cannot replace the master data file
If only admin_password is set (or neither), everyone gets full access -
same as before, for backward compatibility.

LIMITATIONS (be aware of these):
  - Streamlit Cloud's disk is not permanent - if the app restarts (redeploy,
    long idle period, etc.) shared_data/ is wiped and you'll need to
    re-upload once. Your Delay/Received/Bin marks would reset too.
  - Two people editing the exact same row at the exact same second could
    overwrite each other (last save wins). Fine for a small team, not built
    for heavy simultaneous use.
  - Updates aren't instantly pushed to other open tabs - a person sees the
    latest shared state whenever THEY next interact with or reload the page.
"""

import io
import os
import json
import streamlit as st
import pandas as pd

st.set_page_config(page_title="PO Delivery Tracker", layout="wide")

SHARED_DIR = "shared_data"
SHARED_XLSX_PATH = os.path.join(SHARED_DIR, "tracker.xlsx")
SHARED_STATE_PATH = os.path.join(SHARED_DIR, "state.json")
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
# SHARED STATE HELPERS (plain JSON file on disk - simple, no database needed)
# ---------------------------------------------------------------------------
def load_shared_state():
    if os.path.exists(SHARED_STATE_PATH):
        try:
            with open(SHARED_STATE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_shared_state(state):
    with open(SHARED_STATE_PATH, "w") as f:
        json.dump(state, f)


def row_default(rid, state, pipeline_delayed):
    return state.setdefault(rid, {
        "status": "Delayed" if pipeline_delayed else "On Time",
        "received": False,
        "binned": False,
        "revised_date": None,
        "reason": None,
    })


# ---------------------------------------------------------------------------
# ACCESS CONTROL - two tiers, backward compatible with a single password
# ---------------------------------------------------------------------------
def check_access():
    try:
        admin_pw = st.secrets.get("admin_password")
    except Exception:
        admin_pw = None
    try:
        viewer_pw = st.secrets.get("viewer_password")
    except Exception:
        viewer_pw = None
    try:
        legacy_pw = st.secrets.get("app_password")
    except Exception:
        legacy_pw = None

    if not admin_pw and not viewer_pw and not legacy_pw:
        return "admin"  # no secrets configured at all - full access, matches old behaviour

    if st.session_state.get("role"):
        return st.session_state["role"]

    pw = st.text_input("Password", type="password")
    if pw:
        if admin_pw and pw == admin_pw:
            st.session_state["role"] = "admin"
            st.rerun()
        elif legacy_pw and pw == legacy_pw:
            st.session_state["role"] = "admin"
            st.rerun()
        elif viewer_pw and pw == viewer_pw:
            st.session_state["role"] = "editor"
            st.rerun()
        else:
            st.error("Wrong password.")
    return None


role = check_access()
if role is None:
    st.stop()
is_admin = role == "admin"


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
# LOAD / UPLOAD DATA (admin only can replace; everyone reads the shared file)
# ---------------------------------------------------------------------------
st.title("📦 PO Delivery Tracker")

if is_admin:
    uploaded = st.file_uploader("Upload PO_Delivery_Tracker_Final.xlsx to update everyone's view", type=["xlsx"])
    if uploaded is not None:
        with open(SHARED_XLSX_PATH, "wb") as f:
            f.write(uploaded.getbuffer())
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

    with st.container(border=True):
        st.markdown(
            f'**{row["Purchase Order"]}** — {row["Item Code"]} · {row.get("SKU Name", "")} '
            f'{badge(f"{s_emoji} {row["Stock Status"]}", s_bg, s_fg)} '
            f'{badge(supplier, "#E8E8E8", "#333333") if supplier else ""}  \n'
            f'Qty: **{fmt_num(row["Qty"])}** &nbsp;·&nbsp; '
            f'Received: **{fmt_num(row["Received Qty"])}** &nbsp;·&nbsp; '
            f'Pending: **{fmt_num(row["Pending Qty"])}** ({pending_type}) &nbsp;·&nbsp; '
            f'Required By: **{fmt_date(row["Required By"])}**',
            unsafe_allow_html=True,
        )

        widths = [1, 1, 0.8, 1.3, 1.5] if show_delay_controls else [1, 1, 0.8]
        cols = st.columns(widths)

        if rstate["status"] == "On Time":
            if cols[0].button("🔴 Mark Delayed", key=f"toggle_{rid}"):
                shared_state[rid]["status"] = "Delayed"
                save_shared_state(shared_state)
                st.rerun()
        else:
            if cols[0].button("🟢 Mark On Time", key=f"toggle_{rid}"):
                shared_state[rid]["status"] = "On Time"
                save_shared_state(shared_state)
                st.rerun()

        recv_label = "✅ Received" if rstate["received"] else "📥 Mark Received"
        if cols[1].button(recv_label, key=f"recv_{rid}"):
            shared_state[rid]["received"] = not rstate["received"]
            save_shared_state(shared_state)
            st.rerun()

        if cols[2].button("🗑️ Bin", key=f"bin_{rid}"):
            shared_state[rid]["binned"] = True
            save_shared_state(shared_state)
            st.rerun()

        if show_delay_controls:
            existing_date = pd.to_datetime(rstate["revised_date"]).date() if rstate["revised_date"] else None
            new_date = cols[3].date_input("Revised Date", value=existing_date, key=f"date_{rid}",
                                           label_visibility="collapsed")
            if new_date != existing_date:
                shared_state[rid]["revised_date"] = new_date.isoformat() if new_date else None
                save_shared_state(shared_state)

            reason_index = REASON_OPTIONS.index(rstate["reason"]) if rstate["reason"] in REASON_OPTIONS else None
            new_reason = cols[4].selectbox("Reason", REASON_OPTIONS, index=reason_index, key=f"reason_{rid}",
                                            placeholder="Reason", label_visibility="collapsed")
            if new_reason != rstate["reason"]:
                shared_state[rid]["reason"] = new_reason
                save_shared_state(shared_state)

            if not shared_state[rid]["revised_date"]:
                st.warning("⚠️ Pick a revised delivery date.", icon="⚠️")


# ---------------------------------------------------------------------------
# TAB NAVIGATION
# ---------------------------------------------------------------------------
active = po_master[~po_master["_rid"].map(lambda r: shared_state[r]["binned"])]
on_time_rows = active[active["_rid"].map(lambda r: shared_state[r]["status"] == "On Time")]
delayed_rows = active[active["_rid"].map(lambda r: shared_state[r]["status"] == "Delayed")]
binned_rows = po_master[po_master["_rid"].map(lambda r: shared_state[r]["binned"])]

t_ontime, t_delayed, t_bin, t_stock = st.tabs([
    f"🟢 On Time ({len(on_time_rows)})",
    f"🔴 Delayed ({len(delayed_rows)})",
    f"🗑️ Bin ({len(binned_rows)})",
    "📊 Stressed & Watch SKUs",
])

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
