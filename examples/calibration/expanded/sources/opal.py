def inspect_batches(batches, recover):
    for batch in batches:
        for item in batch:
            if item["state"] != "ready":
                continue
            yield recover(item) if item["kind"] == "remote" else item
