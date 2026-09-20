def drain_queue(queue, lookup, retry):
    result = []
    while queue:
        item = queue.pop(0)
        if item["ready"]:
            value = lookup(item["key"])
            if value is None:
                if item["attempts"] < 2:
                    retry(item)
                continue
            result.append(value)
        elif item["attempts"] < 2:
            retry(item)
    return result
