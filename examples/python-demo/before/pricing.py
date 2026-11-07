"""Tiny pricing module for the honest-tests demo (the "before" version, with the bug)."""


def bulk_discount_rate(qty):
    if qty >= 50:
        return 0.20
    if qty > 10:
        return 0.10
    return 0.0


def order_total(qty, unit_price):
    return qty * unit_price * (1 - bulk_discount_rate(qty))
