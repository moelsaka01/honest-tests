'use strict';
// Tiny cart module for the honest-tests demo (the "before" version, with the bug).

function bulkDiscountRate(qty) {
  if (qty >= 50) {
    return 0.2;
  }
  if (qty > 10) {
    return 0.1;
  }
  return 0;
}

function orderTotal(qty, unitPrice) {
  return qty * unitPrice * (1 - bulkDiscountRate(qty));
}

module.exports = { bulkDiscountRate, orderTotal };
