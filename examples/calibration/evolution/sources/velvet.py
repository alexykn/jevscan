def apply_change(store, item):
    store.write(item)
    store.update_index(item["key"])
    return store.finish()
