"""Utility calculations for Shopify AI Sentinel reports."""


def compute_wow_change(current: float | None, prior: float | None) -> float | None:
    """Compute week-over-week percentage change. Returns None if not calculable."""
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / prior


def compute_pct_of_total(part: float, total: float) -> float:
    """Compute percentage of total. Returns 0 if total is 0."""
    if total == 0:
        return 0.0
    return part / total


def compute_inventory_turnover(units_sold: float, inventory_qty: float) -> float:
    """Compute inventory turnover ratio. Returns 0 if no inventory."""
    if inventory_qty == 0:
        return 0.0
    return units_sold / inventory_qty


def compute_return_rate(returns: float, orders: float) -> float:
    """Compute return rate as a decimal (0.0–1.0).

    Shopify's `returns` metric is a refund currency amount (often negative).
    We take abs() to handle the sign convention and clamp to [0, 1] to guard
    against semantic mismatches (e.g. currency divided by count).
    """
    if orders == 0:
        return 0.0
    rate = abs(returns) / orders
    if rate > 1.0:
        # Currency amount ÷ order count produces nonsensical values.
        # Fall back to 0 rather than display a misleading rate.
        return 0.0
    return rate
