def sync_entry(entry, account, ledger, notify):
    if entry["role"] == "owner":
        account["access"] = "full"
        ledger.append(("access", entry["user"]))
    elif entry["role"] == "viewer":
        account["access"] = "read"
        if entry["expired"]:
            account["suspended"] = True
            notify("expired")
    if entry["amount"] > 0:
        account["balance"] -= entry["amount"]
        ledger.append(("charge", entry["amount"]))
        if account["balance"] < 0:
            notify("payment-failed")
    elif entry["cancel"]:
        account["status"] = "cancelled"
        notify("cancelled")
    return account
