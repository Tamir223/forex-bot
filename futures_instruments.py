"""
futures_instruments.py — Futures Contract Specifications
TNL Trader — Phase 2
"""

FUTURES_SPECS = {
    # E-mini S&P 500
    "ES": {
        "name": "E-mini S&P 500",
        "exchange": "CME",
        "point_value": 50.0,       # $50 per point
        "tick_size": 0.25,
        "tick_value": 12.50,
        "typical_sl_pts": 8.0,     # typical stop in points
        "session_close": "16:00",  # CT
        "asset_class": "index",
    },
    # Micro E-mini S&P 500
    "MES": {
        "name": "Micro E-mini S&P 500",
        "exchange": "CME",
        "point_value": 5.0,
        "tick_size": 0.25,
        "tick_value": 1.25,
        "typical_sl_pts": 8.0,
        "session_close": "16:00",
        "asset_class": "index",
    },
    # E-mini Nasdaq 100
    "NQ": {
        "name": "E-mini Nasdaq 100",
        "exchange": "CME",
        "point_value": 20.0,
        "tick_size": 0.25,
        "tick_value": 5.0,
        "typical_sl_pts": 20.0,
        "session_close": "16:00",
        "asset_class": "index",
    },
    # Micro E-mini Nasdaq 100
    "MNQ": {
        "name": "Micro E-mini Nasdaq 100",
        "exchange": "CME",
        "point_value": 2.0,
        "tick_size": 0.25,
        "tick_value": 0.50,
        "typical_sl_pts": 20.0,
        "session_close": "16:00",
        "asset_class": "index",
    },
    # E-mini Russell 2000
    "RTY": {
        "name": "E-mini Russell 2000",
        "exchange": "CME",
        "point_value": 50.0,
        "tick_size": 0.10,
        "tick_value": 5.0,
        "typical_sl_pts": 5.0,
        "session_close": "16:00",
        "asset_class": "index",
    },
    # E-mini Dow Jones
    "YM": {
        "name": "E-mini Dow Jones",
        "exchange": "CBOT",
        "point_value": 5.0,
        "tick_size": 1.0,
        "tick_value": 5.0,
        "typical_sl_pts": 50.0,
        "session_close": "16:00",
        "asset_class": "index",
    },
    # Crude Oil
    "CL": {
        "name": "Crude Oil",
        "exchange": "NYMEX",
        "point_value": 1000.0,
        "tick_size": 0.01,
        "tick_value": 10.0,
        "typical_sl_pts": 0.50,
        "session_close": "14:30",
        "asset_class": "commodity",
    },
    # Micro Crude Oil
    "MCL": {
        "name": "Micro Crude Oil",
        "exchange": "NYMEX",
        "point_value": 100.0,
        "tick_size": 0.01,
        "tick_value": 1.0,
        "typical_sl_pts": 0.50,
        "session_close": "14:30",
        "asset_class": "commodity",
    },
    # Gold
    "GC": {
        "name": "Gold",
        "exchange": "COMEX",
        "point_value": 100.0,
        "tick_size": 0.10,
        "tick_value": 10.0,
        "typical_sl_pts": 5.0,
        "session_close": "13:30",
        "asset_class": "commodity",
    },
    # Micro Gold
    "MGC": {
        "name": "Micro Gold",
        "exchange": "COMEX",
        "point_value": 10.0,
        "tick_size": 0.10,
        "tick_value": 1.0,
        "typical_sl_pts": 5.0,
        "session_close": "13:30",
        "asset_class": "commodity",
    },
    # Natural Gas
    "NG": {
        "name": "Natural Gas",
        "exchange": "NYMEX",
        "point_value": 10000.0,
        "tick_size": 0.001,
        "tick_value": 10.0,
        "typical_sl_pts": 0.05,
        "session_close": "14:30",
        "asset_class": "commodity",
    },
}

FUTURES_SYMBOLS = set(FUTURES_SPECS.keys())


def is_futures(symbol: str) -> bool:
    return symbol.upper() in FUTURES_SYMBOLS


def get_spec(symbol: str) -> dict | None:
    return FUTURES_SPECS.get(symbol.upper())


def calculate_contracts(
    risk_dollars: float,
    sl_points: float,
    symbol: str,
    max_contracts: int = None
) -> dict:
    """
    Calculate how many contracts to trade based on dollar risk and stop size.
    Returns dict with full sizing breakdown.
    """
    spec = get_spec(symbol)
    if not spec or sl_points <= 0:
        return {"contracts": None, "risk_per_contract": None, "total_risk": None}

    risk_per_contract = sl_points * spec["point_value"]
    raw_contracts = risk_dollars / risk_per_contract
    contracts = max(1, round(raw_contracts))

    if max_contracts:
        contracts = min(contracts, max_contracts)

    total_risk = contracts * risk_per_contract

    return {
        "contracts": contracts,
        "risk_per_contract": round(risk_per_contract, 2),
        "total_risk": round(total_risk, 2),
        "point_value": spec["point_value"],
        "symbol": symbol.upper(),
        "name": spec["name"],
    }


def format_sizing(sizing: dict, account_size: float) -> str:
    if not sizing or not sizing.get("contracts"):
        return "--"
    contracts = sizing["contracts"]
    total_risk = sizing["total_risk"]
    risk_pct = round((total_risk / account_size) * 100, 2) if account_size else 0
    label = "contract" if contracts == 1 else "contracts"
    return f"{contracts} {label} (${total_risk:,.0f} risk / {risk_pct}%)"
