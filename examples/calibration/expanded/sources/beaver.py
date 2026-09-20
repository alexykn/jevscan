def _shape_entry(row):
    return {"key": row["key"], "value": row["value"].strip()}


def _store_entries(entries, cache, notify):
    for entry in entries:
        cache.put(entry["key"], entry)
        notify({"kind": "indexed", "key": entry["key"]})


def assemble_digest(rows, cache, notify):
    entries = [_shape_entry(row) for row in rows]
    _store_entries(entries, cache, notify)
    return {"count": len(entries), "entries": entries}
