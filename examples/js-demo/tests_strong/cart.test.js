'use strict';
// Strengthened tests: exact values at the boundaries that the diff touched.
const test = require('node:test');
const assert = require('node:assert');
const cart = require('../cart');

test('discount rate boundaries', () => {
  const cases = [[1, 0], [9, 0], [10, 0.1], [11, 0.1], [49, 0.1], [50, 0.2], [51, 0.2]];
  for (const [qty, rate] of cases) {
    assert.strictEqual(cart.bulkDiscountRate(qty), rate, `qty=${qty}`);
  }
});

test('no discount below ten units', () => { assert.strictEqual(cart.orderTotal(9, 10), 90); });
test('exactly ten units get ten percent off', () => { // fails on the old `qty > 10` code
  assert.strictEqual(cart.orderTotal(10, 10), 90);
});
test('fifty units get twenty percent off', () => { assert.strictEqual(cart.orderTotal(50, 10), 400); });
test('rounds to cents', () => {
  assert.strictEqual(cart.orderTotal(10, 3.333), 30);   // 29.997 -> 30
  assert.strictEqual(cart.orderTotal(10, 3.35), 30.15); // 30.15 stays 30.15
});
test('single unit uses the unit price', () => { assert.strictEqual(cart.orderTotal(1, 7.5), 7.5); });
test('zero and negative qty are rejected', () => {
  for (const qty of [0, -1]) {
    assert.throws(() => cart.orderTotal(qty, 5), RangeError);
  }
});

test('shipping fee below the threshold', () => { assert.strictEqual(cart.shippingCost(99.99), 7); });
test('free shipping at and above the threshold', () => {
  assert.strictEqual(cart.shippingCost(100), 0);
  assert.strictEqual(cart.shippingCost(250), 0);
});

test('valid coupon takes five off', () => { assert.strictEqual(cart.applyCoupon(100, 'SAVE5'), 95); });
test('coupon needs more than twenty', () => {
  assert.strictEqual(cart.applyCoupon(20, 'SAVE5'), 20);
  assert.strictEqual(cart.applyCoupon(21, 'SAVE5'), 16);
});
test('wrong code changes nothing', () => { assert.strictEqual(cart.applyCoupon(100, 'NOPE'), 100); });
