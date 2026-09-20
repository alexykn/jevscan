def sync_entry(entry, cache, audit, send):
    value = entry["value"]
    if value < 0:
        raise ValueError("range")
    cache.put(entry["key"], value)
    audit.write(entry["key"], value)
    send(entry["key"])
    return value
