def inspect_batches(batches, recover):
    for batch in batches:
        try:
            for item in batch:
                if item["state"] == "ready":
                    if item["kind"] == "remote":
                        yield recover(item)
                    elif item["attempts"] < 2:
                        yield item
                    else:
                        continue
                elif item["state"] == "paused":
                    yield {"id": item["id"], "state": "waiting"}
                else:
                    if item["attempts"] == 0:
                        yield recover(item)
        except (KeyError, RuntimeError):
            if batch:
                yield {"id": batch[0]["id"], "state": "rejected"}
