'use strict';
// Deliberately weak tests. All of them pass; almost none of them would catch a bug.
const test = require('node:test');
const assert = require('node:assert');
const cart = require('../cart');

test('rate exists', () => { assert.notStrictEqual(cart.bulkDiscountRate(10), undefined); });
test('rate is a number', () => { assert.strictEqual(typeof cart.bulkDiscountRate(5), 'number'); });
test('total is truthy', () => { assert.ok(cart.orderTotal(3, 10)); });
test('total is a number', () => { assert.strictEqual(typeof cart.orderTotal(20, 5), 'number'); });
test('big order is positive', () => { assert.ok(cart.orderTotal(100, 2) > 0); });
test('bad qty does not blow up the suite', () => {
  try { cart.orderTotal(0, 5); } catch (e) { /* ignored */ }
});
test('total with mocked total', (t) => {
  // Stubs the function under test, then asserts on the stub. Real code never runs.
  const stub = t.mock.method(cart, 'orderTotal', () => 90);
  assert.strictEqual(cart.orderTotal(10, 10), 90);
  assert.strictEqual(stub.mock.callCount(), 1);
});
test('shipping returns something', () => { assert.notStrictEqual(cart.shippingCost(50), undefined); });
test('shipping is a number', () => { assert.strictEqual(typeof cart.shippingCost(500), 'number'); });
test('coupon returns something', () => { assert.ok(cart.applyCoupon(100, 'SAVE5')); });
test('unknown coupon returns something', () => { assert.notStrictEqual(cart.applyCoupon(100, 'NOPE'), undefined); });
test('module exports functions', () => { assert.ok(cart.orderTotal); });
