import streamlit as st
import sqlite3
import requests
import math
from datetime import date, datetime, timedelta

# ============================================================
# MUTUAL FUND NAV TRACKER + SIP XIRR CALCULATOR
# ============================================================

DB_NAME = "mutual_fund.db"
AMFI_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"

st.set_page_config(
    page_title="Mutual Fund NAV Tracker",
    page_icon="📈",
    layout="wide"
)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return sqlite3.connect(DB_NAME)


def setup_database():
    conn = get_db()
    cur = conn.cursor()

    # Create schemes table if it doesn't exist
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schemes (
            scheme_id INTEGER PRIMARY KEY AUTOINCREMENT,
            amfi_code TEXT UNIQUE,
            scheme_name TEXT
        )
    """)

    # Create NAV history
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nav_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheme_id INTEGER NOT NULL,
            nav_date TEXT NOT NULL,
            nav REAL NOT NULL,
            UNIQUE(scheme_id, nav_date),
            FOREIGN KEY(scheme_id) REFERENCES schemes(scheme_id)
        )
    """)

    # Our application transactions table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sip_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheme_id INTEGER NOT NULL,
            transaction_date TEXT NOT NULL,
            amount REAL NOT NULL,
            units REAL NOT NULL,
            FOREIGN KEY(scheme_id) REFERENCES schemes(scheme_id)
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_nav_scheme_date
        ON nav_history(scheme_id, nav_date)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_scheme_code
        ON schemes(amfi_code)
    """)

    conn.commit()
    conn.close()


setup_database()


# ============================================================
# AMFI DOWNLOAD
# ============================================================

def download_amfi(start_date, end_date):
    """
    Download AMFI historical NAV data.
    """

    params = {
        "frmdt": start_date.strftime("%d-%b-%Y"),
        "todt": end_date.strftime("%d-%b-%Y"),
        "tp": "1"
    }

    response = requests.get(
        AMFI_URL,
        params=params,
        timeout=120,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )

    response.raise_for_status()

    return response.text


# ============================================================
# IMPORT NAV DATA
# ============================================================

def import_nav_data(text):
    conn = get_db()
    cur = conn.cursor()

    parsed = 0
    matched = 0
    inserted = 0
    skipped = 0

    rows_to_insert = []

    for line in text.splitlines():

        line = line.strip()

        if not line:
            continue

        parts = line.split(";")

        if len(parts) < 8:
            skipped += 1
            continue

        scheme_code = parts[0].strip()
        scheme_name = parts[1].strip()
        nav_text = parts[6].strip()
        nav_date = parts[7].strip()

        # Ignore headers
        if scheme_code.lower() in (
            "scheme code",
            "scheme_code",
            "schemecode"
        ):
            continue

        # Ignore non-numeric scheme codes
        if not scheme_code.isdigit():
            skipped += 1
            continue

        try:
            nav = float(nav_text)
        except (ValueError, TypeError):
            skipped += 1
            continue

        if nav <= 0:
            skipped += 1
            continue

        # ----------------------------------------------------
        # IMPORTANT:
        # Match using AMFI CODE, NOT scheme name.
        # This handles scheme name changes / mergers.
        # ----------------------------------------------------

        cur.execute("""
            SELECT scheme_id
            FROM schemes
            WHERE amfi_code = ?
        """, (scheme_code,))

        result = cur.fetchone()

        # If scheme doesn't exist, add it
        if result is None:

            try:
                cur.execute("""
                    INSERT INTO schemes
                    (amfi_code, scheme_name)
                    VALUES (?, ?)
                """, (
                    scheme_code,
                    scheme_name
                ))

                scheme_id = cur.lastrowid
                matched += 1

            except sqlite3.IntegrityError:

                cur.execute("""
                    SELECT scheme_id
                    FROM schemes
                    WHERE amfi_code = ?
                """, (scheme_code,))

                result = cur.fetchone()

                if result is None:
                    skipped += 1
                    continue

                scheme_id = result[0]

        else:
            scheme_id = result[0]
            matched += 1

        parsed += 1

        rows_to_insert.append((
            scheme_id,
            nav_date,
            nav
        ))

    # Insert in batches
    cur.executemany("""
        INSERT OR IGNORE INTO nav_history
        (
            scheme_id,
            nav_date,
            nav
        )
        VALUES (?, ?, ?)
    """, rows_to_insert)

    inserted = cur.rowcount

    conn.commit()
    conn.close()

    return {
        "parsed": parsed,
        "matched": matched,
        "inserted": inserted,
        "skipped": skipped
    }


# ============================================================
# SCHEME SEARCH
# ============================================================

def search_schemes(search_text):
    conn = get_db()

    cur = conn.cursor()

    cur.execute("""
        SELECT
            scheme_id,
            amfi_code,
            scheme_name
        FROM schemes
        WHERE scheme_name LIKE ?
           OR amfi_code LIKE ?
        ORDER BY scheme_name
        LIMIT 100
    """, (
        "%" + search_text + "%",
        "%" + search_text + "%"
    ))

    rows = cur.fetchall()

    conn.close()

    return rows


# ============================================================
# GET NAV
# ============================================================

def get_nav(scheme_id, transaction_date):
    conn = get_db()

    cur = conn.cursor()

    cur.execute("""
        SELECT nav
        FROM nav_history
        WHERE scheme_id = ?
          AND nav_date <= ?
        ORDER BY nav_date DESC
        LIMIT 1
    """, (
        scheme_id,
        transaction_date
    ))

    result = cur.fetchone()

    conn.close()

    if result:
        return float(result[0])

    return None


def get_latest_nav(scheme_id):
    conn = get_db()

    cur = conn.cursor()

    cur.execute("""
        SELECT nav_date, nav
        FROM nav_history
        WHERE scheme_id = ?
        ORDER BY nav_date DESC
        LIMIT 1
    """, (scheme_id,))

    result = cur.fetchone()

    conn.close()

    return result


# ============================================================
# XIRR
# ============================================================

def xnpv(rate, cashflows):

    first_date = cashflows[0][0]

    total = 0.0

    for d, amount in cashflows:

        days = (d - first_date).days

        total += amount / (
            (1 + rate) ** (days / 365.0)
        )

    return total


def calculate_xirr(cashflows):

    if len(cashflows) < 2:
        return None

    has_positive = any(x[1] > 0 for x in cashflows)
    has_negative = any(x[1] < 0 for x in cashflows)

    if not has_positive or not has_negative:
        return None

    # Try Newton's method first
    rate = 0.10

    for _ in range(100):

        try:

            first_date = cashflows[0][0]

            npv = 0.0
            derivative = 0.0

            for d, amount in cashflows:

                days = (d - first_date).days
                years = days / 365.0

                denominator = (1 + rate) ** years

                npv += amount / denominator

                if rate != -1:
                    derivative -= (
                        years * amount /
                        ((1 + rate) ** (years + 1))
                    )

            if abs(npv) < 0.000001:
                return rate

            if derivative == 0:
                break

            new_rate = rate - npv / derivative

            if new_rate <= -0.999999:
                break

            rate = new_rate

        except (OverflowError, ZeroDivisionError):
            break

    # --------------------------------------------------------
    # Fallback: Bisection
    # This helps with irregular cash flows where Newton
    # sometimes fails to converge.
    # --------------------------------------------------------

    low = -0.9999
    high = 10.0

    low_value = xnpv(low, cashflows)
    high_value = xnpv(high, cashflows)

    # Expand upper bound if necessary
    for _ in range(20):

        if low_value * high_value <= 0:
            break

        high *= 2

        try:
            high_value = xnpv(high, cashflows)
        except OverflowError:
            break

    if low_value * high_value > 0:
        return None

    for _ in range(200):

        mid = (low + high) / 2

        try:
            mid_value = xnpv(mid, cashflows)
        except (OverflowError, ZeroDivisionError):
            return None

        if abs(mid_value) < 0.000001:
            return mid

        if low_value * mid_value <= 0:
            high = mid
            high_value = mid_value
        else:
            low = mid
            low_value = mid_value

    return (low + high) / 2


# ============================================================
# SAVE TRANSACTION
# ============================================================

def save_transaction(scheme_id, transaction_date, amount):

    nav = get_nav(
        scheme_id,
        transaction_date
    )

    if nav is None:
        return False, "NAV not available for this date."

    units = amount / nav

    conn = get_db()

    cur = conn.cursor()

    cur.execute("""
        INSERT INTO sip_transactions
        (
            scheme_id,
            transaction_date,
            amount,
            units
        )
        VALUES (?, ?, ?, ?)
    """, (
        scheme_id,
        transaction_date,
        amount,
        units
    ))

    conn.commit()
    conn.close()

    return True, units


# ============================================================
# GET TRANSACTIONS
# ============================================================

def get_transactions():

    conn = get_db()

    cur = conn.cursor()

    cur.execute("""
        SELECT
            t.id,
            t.scheme_id,
            s.amfi_code,
            s.scheme_name,
            t.transaction_date,
            t.amount,
            t.units
        FROM sip_transactions t
        JOIN schemes s
          ON t.scheme_id = s.scheme_id
        ORDER BY t.transaction_date
    """)

    rows = cur.fetchall()

    conn.close()

    return rows


# ============================================================
# MAIN UI
# ============================================================

st.title("📈 Mutual Fund NAV Tracker & SIP Return Calculator")

st.caption(
    "AMFI NAV tracking • SIP history • XIRR • "
    "Scheme-code based matching"
)


# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.title("Navigation")

page = st.sidebar.radio(
    "Go to",
    [
        "Dashboard",
        "Download NAV",
        "Scheme Search",
        "Add SIP",
        "SIP Returns"
    ]
)


# ============================================================
# DASHBOARD
# ============================================================

if page == "Dashboard":

    st.header("Dashboard")

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM schemes")
    schemes_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM nav_history")
    nav_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM sip_transactions")
    transaction_count = cur.fetchone()[0]

    conn.close()

    col1, col2, col3 = st.columns(3)

    col1.metric(
        "Mutual Fund Schemes",
        f"{schemes_count:,}"
    )

    col2.metric(
        "NAV History Records",
        f"{nav_count:,}"
    )

    col3.metric(
        "SIP Transactions",
        f"{transaction_count:,}"
    )

    st.divider()

    if nav_count == 0:
        st.info(
            "No NAV history exists yet. "
            "Go to 'Download NAV' and import historical data."
        )

    else:

        conn = get_db()

        cur = conn.cursor()

        cur.execute("""
            SELECT
                MIN(nav_date),
                MAX(nav_date)
            FROM nav_history
        """)

        dates = cur.fetchone()

        conn.close()

        st.subheader("NAV Database")

        st.write(
            f"Available NAV range: **{dates[0]} → {dates[1]}**"
        )


# ============================================================
# DOWNLOAD NAV
# ============================================================

elif page == "Download NAV":

    st.header("⬇️ Download NAV from AMFI")

    st.write(
        "Download historical daily NAV data directly from AMFI."
    )

    col1, col2 = st.columns(2)

    start_date = col1.date_input(
        "From date",
        value=date(2025, 1, 1)
    )

    end_date = col2.date_input(
        "To date",
        value=date(2025, 1, 31)
    )

    if st.button(
        "Download & Import NAV",
        type="primary"
    ):

        if start_date > end_date:

            st.error(
                "From date cannot be after To date."
            )

        else:

            with st.spinner(
                "Downloading NAV data from AMFI..."
            ):

                try:

                    text = download_amfi(
                        start_date,
                        end_date
                    )

                    st.success(
                        f"Downloaded {len(text):,} characters."
                    )

                    result = import_nav_data(text)

                    st.success(
                        "NAV import completed."
                    )

                    col1, col2, col3, col4 = st.columns(4)

                    col1.metric(
                        "Parsed",
                        f"{result['parsed']:,}"
                    )

                    col2.metric(
                        "Matched",
                        f"{result['matched']:,}"
                    )

                    col3.metric(
                        "Inserted",
                        f"{result['inserted']:,}"
                    )

                    col4.metric(
                        "Skipped",
                        f"{result['skipped']:,}"
                    )

                    st.info(
                        "Schemes are matched using the AMFI scheme code, "
                        "not the scheme name. This allows scheme names "
                        "to change without breaking historical records."
                    )

                except Exception as e:

                    st.error(
                        f"Download/import failed: {e}"
                    )


# ============================================================
# SCHEME SEARCH
# ============================================================

elif page == "Scheme Search":

    st.header("🔎 Search Mutual Funds")

    search = st.text_input(
        "Enter scheme name or AMFI code",
        placeholder="Example: Axis, Tata, 139619"
    )

    if search:

        results = search_schemes(search)

        if not results:

            st.warning(
                "No matching schemes found."
            )

        else:

            st.write(
                f"Found {len(results)} matching schemes."
            )

            for scheme_id, code, name in results:

                with st.container(border=True):

                    st.write(
                        f"**{name}**"
                    )

                    st.caption(
                        f"AMFI Code: {code} | Scheme ID: {scheme_id}"
                    )

                    latest = get_latest_nav(scheme_id)

                    if latest:

                        st.write(
                            f"Latest NAV: **₹{latest[1]:.4f}** "
                            f"({latest[0]})"
                        )


# ============================================================
# ADD SIP
# ============================================================

elif page == "Add SIP":

    st.header("💰 Add SIP Investment")

    st.write(
        "Enter an investment. The application automatically "
        "uses the NAV available on or before the transaction date."
    )

    search = st.text_input(
        "Search fund",
        placeholder="Example: Tata Quant Fund"
    )

    selected_scheme = None

    if search:

        results = search_schemes(search)

        if results:

            options = {
                f"{code} — {name}": (
                    scheme_id,
                    code,
                    name
                )
                for scheme_id, code, name in results
            }

            selected = st.selectbox(
                "Select scheme",
                list(options.keys())
            )

            selected_scheme = options[selected]

        else:

            st.warning(
                "No schemes found. Download NAV data first."
            )

    transaction_date = st.date_input(
        "Investment date",
        value=date.today()
    )

    amount = st.number_input(
        "Investment amount (₹)",
        min_value=1.0,
        value=5000.0,
        step=500.0
    )

    if st.button(
        "Add Investment",
        type="primary"
    ):

        if selected_scheme is None:

            st.error(
                "Please select a mutual fund."
            )

        else:

            scheme_id = selected_scheme[0]

            success, result = save_transaction(
                scheme_id,
                transaction_date.strftime("%d-%b-%Y"),
                amount
            )

            if success:

                st.success(
                    f"Investment added successfully. "
                    f"Units purchased: {result:.6f}"
                )

            else:

                st.error(result)


# ============================================================
# SIP RETURNS
# ============================================================

elif page == "SIP Returns":

    st.header("📊 SIP Returns & XIRR")

    transactions = get_transactions()

    if not transactions:

        st.info(
            "No SIP transactions yet. "
            "Go to 'Add SIP' first."
        )

    else:

        st.subheader("Your Investments")

        total_invested = 0.0
        total_current_value = 0.0

        cashflows = []

        for (
            transaction_id,
            scheme_id,
            code,
            name,
            transaction_date,
            amount,
            units
        ) in transactions:

            total_invested += amount

            try:
                d = datetime.strptime(
                    transaction_date,
                    "%d-%b-%Y"
                ).date()
            except ValueError:
                d = datetime.strptime(
                    transaction_date,
                    "%Y-%m-%d"
                ).date()

            latest = get_latest_nav(scheme_id)

            current_value = 0.0

            if latest:
                current_nav = float(latest[1])
                current_value = units * current_nav

            total_current_value += current_value

            cashflows.append(
                (d, -float(amount))
            )

            with st.container(border=True):

                st.write(
                    f"**{name}**"
                )

                col1, col2, col3, col4 = st.columns(4)

                col1.write(
                    f"Date\n\n{transaction_date}"
                )

                col2.write(
                    f"Invested\n\n₹{amount:,.2f}"
                )

                col3.write(
                    f"Units\n\n{units:.6f}"
                )

                col4.write(
                    f"Current value\n\n₹{current_value:,.2f}"
                )

        # ----------------------------------------------------
        # Add current portfolio value as final positive cashflow
        # ----------------------------------------------------

        cashflows.append(
            (date.today(), total_current_value)
        )

        xirr = calculate_xirr(
            sorted(cashflows, key=lambda x: x[0])
        )

        st.divider()

        col1, col2, col3 = st.columns(3)

        col1.metric(
            "Total Invested",
            f"₹{total_invested:,.2f}"
        )

        col2.metric(
            "Current Value",
            f"₹{total_current_value:,.2f}"
        )

        profit = (
            total_current_value -
            total_invested
        )

        col3.metric(
            "Profit / Loss",
            f"₹{profit:,.2f}"
        )

        st.divider()

        if xirr is not None:

            st.subheader("XIRR")

            st.metric(
                "Annualized Return",
                f"{xirr * 100:.2f}%"
            )

            st.caption(
                "XIRR accounts for the actual dates of "
                "irregular cash flows."
            )

        else:

            st.warning(
                "XIRR could not be calculated. "
                "Make sure there are both investments and "
                "a current portfolio value."
            )


# ============================================================
# FOOTER
# ============================================================

st.sidebar.divider()

st.sidebar.caption(
    "Mutual Fund NAV Tracker & SIP Return Calculator"
)

st.sidebar.caption(
    "NAV source: AMFI"
)
