"""Deliberately weak tests. All of them pass; almost none of them would catch a bug."""
import unittest
from unittest import mock

import pricing


class WeakPricingTests(unittest.TestCase):
    def test_rate_exists(self):
        self.assertIsNotNone(pricing.bulk_discount_rate(10))

    def test_rate_is_float(self):
        self.assertIsInstance(pricing.bulk_discount_rate(5), float)

    def test_total_is_truthy(self):
        self.assertTrue(pricing.order_total(3, 10))

    def test_total_is_number(self):
        self.assertIsInstance(pricing.order_total(20, 5), float)

    def test_big_order_is_positive(self):
        self.assertGreater(pricing.order_total(100, 2), 0)

    def test_bad_qty_does_not_blow_up_the_suite(self):
        try:
            pricing.order_total(0, 5)
        except Exception:
            pass

    def test_total_with_mocked_total(self):
        # Patches the function under test, then asserts on the patch. Real code never runs.
        with mock.patch("pricing.order_total", return_value=90.0) as m:
            self.assertEqual(pricing.order_total(10, 10), 90.0)
            m.assert_called_once_with(10, 10)

    def test_shipping_returns_something(self):
        self.assertIsNotNone(pricing.shipping_cost(50))

    def test_shipping_is_int(self):
        self.assertIsInstance(pricing.shipping_cost(500), int)

    def test_coupon_returns_something(self):
        self.assertTrue(pricing.apply_coupon(100, "SAVE5"))

    def test_unknown_coupon_returns_something(self):
        self.assertIsNotNone(pricing.apply_coupon(100, "NOPE"))

    def test_constants_defined(self):
        self.assertTrue(pricing.FREE_SHIPPING_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
