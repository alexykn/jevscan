async def dispatch_update(update, deliver):
    if update is None:
        return False
    await deliver(update)
    return True
