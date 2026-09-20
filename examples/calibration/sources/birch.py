def publish_receipt(order, store, renderer):
    receipt = store.insert(order)
    store.append_audit({"order_id": receipt["id"], "actor": order["actor"]})
    return renderer.render(receipt)
