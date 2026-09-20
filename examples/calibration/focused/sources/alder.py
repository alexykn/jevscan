async def coordinate_batch(items, ledger, notify):
    total = 0
    for item in items:
        if item["amount"] <= 0:
            continue
        total += item["amount"]
        ledger[item["key"]] = item["amount"]
        await notify(item["key"])
    return total
