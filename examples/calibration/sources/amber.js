export function publishReceipt(order, store, renderer) {
  const receipt = store.insert(order);
  const audit = { orderId: receipt.id, actor: order.actor };
  store.appendAudit(audit);
  const title = renderer.title(order.customer);
  return `<article><h1>${title}</h1><p>${receipt.total}</p></article>`;
}
