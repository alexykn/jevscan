def store_record(record, store):
    if record["version"] < 1:
        raise ValueError("version")
    store.write(record)
    if record["version"] < 1:
        raise ValueError("version")
    return record["version"]
