def assemble_digest(rows, cache, notify):
    entries = []
    for row in rows:
        entry = {"key": row["key"], "value": row["value"].strip()}
        entries.append(entry)
        cache.put(entry["key"], entry)
        notify({"kind": "indexed", "key": entry["key"]})
    return {"count": len(entries), "entries": entries}
