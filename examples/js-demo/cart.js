'use strict';
// Tiny cart module for the honest-tests demo (the "after" version).

const FREE_SHIPPING_THRESHOLD = 100;
const SHIPPING_FEE = 7;

function bulkDiscountRate(qty) {
  if (qty >= 50) {
    return 0.2;
  }
  if (qty >= 10) { // the bug fix: this used to be `qty > 10`, so exactly 10 units got no discount
    return 0.1;
  }
  return 0;
}

function orderTotal(qty, unitPrice) {
  if (qty <= 0) {
    throw new RangeError('qty must be positive');
  }
  const subtotal = qty * unitPrice;
  const discounted = subtotal * (1 - bulkDiscountRate(qty));
  return Math.round(discounted * 100) / 100;
}

function shippingCost(total) {
  if (total >= FREE_SHIPPING_THRESHOLD) {
    return 0;
  }
  return SHIPPING_FEE;
}

function applyCoupon(total, code) {
  if (code === 'SAVE5' && total > 20) {
    return total - 5;
  }
  return total;
}

module.exports = { bulkDiscountRate, orderTotal, shippingCost, applyCoupon };
