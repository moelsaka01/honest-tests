"""Tiny pricing module for the honest-tests demo (the "after" version)."""

FREE_SHIPPING_THRESHOLD = 100
SHIPPING_FEE = 7


def bulk_discount_rate(qty):
    """20% off from 50 units, 10% off from 10 units."""
    if qty >= 50:
        return 0.20
    if qty >= 10:  # the bug fix: this used to be `qty > 10`, so exactly 10 units got no discount
        return 0.10
    return 0.0


def order_total(qty, unit_price):
    if qty <= 0:
        raise ValueError("qty must be positive")
    subtotal = qty * unit_price
    discounted = subtotal * (1 - bulk_discount_rate(qty))
    return round(discounted, 2)


def shipping_cost(total):
    if total >= FREE_SHIPPING_THRESHOLD:
        return 0
    return SHIPPING_FEE


def apply_coupon(total, code):
    if code == "SAVE5" and total > 20:
        return total - 5
    return total
