def advance_state(event, state, save, emit):
    if event.kind == "open":
        if state["closed"]:
            state["closed"] = False
            save(state)
        else:
            emit("already-open")
    elif event.kind == "close":
        if not state["closed"]:
            state["closed"] = True
            save(state)
        else:
            emit("already-closed")
    return state
