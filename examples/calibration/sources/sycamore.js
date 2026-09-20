export function reconcileOrder(order, policy, inventory) {
  const decision = policy.evaluate(order);
  return inventory.apply(order, decision);
}
