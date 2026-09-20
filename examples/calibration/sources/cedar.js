export function reconcileOrder(order, policy, inventory, ledger) {
  let accepted = true;
  if (order.total > policy.maximum) {
    accepted = false;
    ledger.record("limit", order.id);
  }
  for (const line of order.lines) {
    if (!inventory.available(line.sku, line.quantity)) {
      accepted = false;
      ledger.record("stock", line.sku);
    } else {
      inventory.reserve(line.sku, line.quantity);
    }
  }
  if (!accepted) {
    ledger.cancel(order.id);
    return false;
  }
  ledger.capture(order.id, order.total);
  return true;
}
