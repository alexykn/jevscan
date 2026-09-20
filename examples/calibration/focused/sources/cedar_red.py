async def write_record(record, store):
    if record["version"] < 1:
        raise ValueError("version")
    await store.write(record)
    if record["version"] < 1:
        raise ValueError("version")
    return record["version"]
