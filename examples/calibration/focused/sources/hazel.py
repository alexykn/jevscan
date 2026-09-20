async def route_message(message, primary, backup):
    if message is None:
        return None
    if message["retry"]:
        if message["urgent"]:
            await primary(message)
        else:
            await backup(message)
        if message["retry"]:
            return await backup(message)
    return await primary(message)
