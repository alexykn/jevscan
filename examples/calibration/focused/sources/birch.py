def record_snapshot(entries, store, publish):
    snapshot = []
    for entry in entries:
        value = entry["value"]
        if value is None:
            continue
        snapshot.append(value)
        store.save(entry["key"], value)
        publish(entry["key"], value)
    return snapshot
