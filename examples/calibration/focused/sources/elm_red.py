def apply_change(item, transform):
    if item["state"] != "ready":
        raise ValueError("state")
    transform(item)
    if item["state"] != "ready":
        raise ValueError("state")
    return item
