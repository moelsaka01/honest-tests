"""Strengthened tests: exact values at the boundaries that the diff touched."""
import unittest

import pricing


class DiscountRateTests(unittest.TestCase):
    def test_boundaries(self):
        cases = {1: 0.0, 9: 0.0, 10: 0.10, 11: 0.10, 49: 0.10, 50: 0.20, 51: 0.20}
        for qty, rate in cases.items():
            with self.subTest(qty=qty):
                self.assertEqual(pricing.bulk_discount_rate(qty), rate)


class OrderTotalTests(unittest.TestCase):
    def test_no_discount_below_ten(self):
        self.assertEqual(pricing.order_total(9, 10), 90.0)

    def test_exactly_ten_units_get_ten_percent_off(self):  # fails on the old `qty > 10` code
        self.assertEqual(pricing.order_total(10, 10), 90.0)

    def test_fifty_units_get_twenty_percent_off(self):
        self.assertEqual(pricing.order_total(50, 10), 400.0)

    def test_rounds_to_cents(self):
        self.assertEqual(pricing.order_total(10, 3.333), 30.0)   # 29.997: three digits would differ
        self.assertEqual(pricing.order_total(10, 3.35), 30.15)   # 30.15: one digit would differ

    def test_single_unit_uses_unit_price(self):
        self.assertEqual(pricing.order_total(1, 7.5), 7.5)

    def test_zero_and_negative_qty_rejected(self):
        for qty in (0, -1):
            with self.subTest(qty=qty), self.assertRaises(ValueError):
                pricing.order_total(qty, 5)


class ShippingTests(unittest.TestCase):
    def test_fee_below_threshold(self):
        self.assertEqual(pricing.shipping_cost(99.99), 7)

    def test_free_at_threshold_and_above(self):
        self.assertEqual(pricing.shipping_cost(100), 0)
        self.assertEqual(pricing.shipping_cost(250), 0)


class CouponTests(unittest.TestCase):
    def test_valid_coupon_takes_five_off(self):
        self.assertEqual(pricing.apply_coupon(100, "SAVE5"), 95)

    def test_coupon_needs_more_than_twenty(self):
        self.assertEqual(pricing.apply_coupon(20, "SAVE5"), 20)
        self.assertEqual(pricing.apply_coupon(21, "SAVE5"), 16)

    def test_wrong_code_changes_nothing(self):
        self.assertEqual(pricing.apply_coupon(100, "NOPE"), 100)


if __name__ == "__main__":
    unittest.main()
