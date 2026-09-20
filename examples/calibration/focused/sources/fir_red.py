def finish_cycle(record, commit):
    if record["state"] != "ready":
        raise ValueError("state")
    commit(record)
    if record["state"] != "ready":
        raise ValueError("state")
    return record
