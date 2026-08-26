"""
app.py — Brillare PO Delivery Tracker
--------------------------------------------------------------------------------
Tab navigation: On Time | Delayed | Bin | Stressed & Watch SKUs.
Each PO line is a compact card: PO/SKU/Qty/Received/Pending on one line,
action buttons right below (Mark Delayed/On Time, Mark Received, Bin).
Delayed cards additionally show a calendar + reason picker inline - since
they're isolated in their own tab, no scrolling past On Time rows to reach them.

WORKFLOW:
  1. Run your pipeline as usual: python run_stock_tracker.py
  2. Open this app, upload PO_Delivery_Tracker_Final.xlsx
  3. Mark delays / received / bin as needed, download Delay_Tracker.xlsx,
     drop it in your 3P Tracker folder before the next run_stock_tracker.py run
"""

import io
import streamlit as st
import pandas as pd

st.set_page_config(page_title="PO Delivery Tracker", layout="wide")


def check_password():
    try:
        has_password = "app_password" in st.secrets
    except Exception:
        has_password = False
    if not has_password:
        return True
    if st.session_state.get("authed", False):
        return True
    pw = st.text_input("Password", type="password")
    if pw == st.secrets["app_password"]:
        st.session_state["authed"] = True
        st.rerun()
    elif pw:
        st.error("Wrong password.")
    return False


if not check_password():
    st.stop()

REASON_OPTIONS = ["RM Connectivity", "PM Connectivity", "Others"]

STOCK_BADGE = {
    "Stressed": ("🔴", "#FFC7CE", "#9C0006"),
    "Watch": ("🟡", "#FFEB9C", "#9C6500"),
    "Relaxed": ("🟢", "#C6EFCE", "#006100"),
    "Low Movement": ("⚪", "#D9D9D9", "#595959"),
    "No DRR Data": ("⚪", "#E9E9E9", "#757575"),
}


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
# LOAD DATA
# ---------------------------------------------------------------------------
st.title("📦 PO Delivery Tracker")

uploaded = st.file_uploader("Upload PO_Delivery_Tracker_Final.xlsx", type=["xlsx"])
if uploaded is None:
    st.info("👈 Upload today's tracker output to get started.")
    st.stop()

try:
    po_master = pd.read_excel(uploaded, sheet_name="PO Master")
    stock_health = pd.read_excel(uploaded, sheet_name="SKU Stock Health")
except Exception as e:
    st.error(f"Couldn't read this file - is it the right output from run_stock_tracker.py? ({e})")
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

# ---------------------------------------------------------------------------
# PER-ROW STATE (button-driven fields; date/reason use native widget keys)
#   Initialized against the FULL po_master (before any filtering below) so
#   that applying/clearing a vendor or search filter never loses state on
#   rows that are temporarily hidden.
# ---------------------------------------------------------------------------
for key in ["row_status", "row_received", "row_binned"]:
    if key not in st.session_state:
        st.session_state[key] = {}

for _, row in po_master.iterrows():
    rid = row["_rid"]
    if rid not in st.session_state.row_status:
        st.session_state.row_status[rid] = "Delayed" if row.get("Delivery Status") == "Delayed" else "On Time"
    st.session_state.row_received.setdefault(rid, False)
    st.session_state.row_binned.setdefault(rid, False)

# ---------------------------------------------------------------------------
# VENDOR + SKU SEARCH FILTER BAR (applies across every tab below)
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

po_master = po_filtered  # everything below (tabs, counts) now respects the filter bar


def render_row(row, show_delay_controls):
    rid = row["_rid"]
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

        ncols = 5 if show_delay_controls else 3
        widths = [1, 1, 0.8, 1.3, 1.5] if show_delay_controls else [1, 1, 0.8]
        cols = st.columns(widths)

        if st.session_state.row_status[rid] == "On Time":
            if cols[0].button("🔴 Mark Delayed", key=f"toggle_{rid}"):
                st.session_state.row_status[rid] = "Delayed"
                st.rerun()
        else:
            if cols[0].button("🟢 Mark On Time", key=f"toggle_{rid}"):
                st.session_state.row_status[rid] = "On Time"
                st.rerun()

        received_now = st.session_state.row_received[rid]
        recv_label = "✅ Received" if received_now else "📥 Mark Received"
        if cols[1].button(recv_label, key=f"recv_{rid}"):
            st.session_state.row_received[rid] = not received_now
            st.rerun()

        if cols[2].button("🗑️ Bin", key=f"bin_{rid}"):
            st.session_state.row_binned[rid] = True
            st.rerun()

        if show_delay_controls:
            cols[3].date_input("Revised Date", value=None, key=f"date_{rid}", label_visibility="collapsed")
            cols[4].selectbox("Reason", REASON_OPTIONS, index=None, key=f"reason_{rid}",
                               placeholder="Reason", label_visibility="collapsed")
            if st.session_state.get(f"date_{rid}") is None:
                st.warning("⚠️ Pick a revised delivery date.", icon="⚠️")


# ---------------------------------------------------------------------------
# TAB NAVIGATION
# ---------------------------------------------------------------------------
active = po_master[~po_master["_rid"].map(st.session_state.row_binned)]
on_time_rows = active[active["_rid"].map(lambda r: st.session_state.row_status[r] == "On Time")]
delayed_rows = active[active["_rid"].map(lambda r: st.session_state.row_status[r] == "Delayed")]
binned_rows = po_master[po_master["_rid"].map(st.session_state.row_binned)]

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
        rid = row["_rid"]
        d = st.session_state.get(f"date_{rid}")
        if d:
            export_rows.append({
                "Purchase Order": row["Purchase Order"],
                "Item Code": row["Item Code"],
                "Revised Expected Delivery Date": d,
                "Delay Reason": st.session_state.get(f"reason_{rid}") or "Others",
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
                st.session_state.row_binned[rid] = False
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
