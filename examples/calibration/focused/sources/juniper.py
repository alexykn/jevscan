def advance_state(event, state, save, emit):
    if event.kind == "open":
        if state["closed"]:
            if state["locked"]:
                emit("locked")
            elif state["retry"]:
                state["closed"] = False
                state["retry"] = False
                save(state)
            else:
                state["closed"] = False
                emit("opened")
        else:
            emit("already-open")
    elif event.kind == "close":
        if not state["closed"]:
            if state["dirty"]:
                save(state)
                state["dirty"] = False
            state["closed"] = True
            save(state)
        else:
            emit("already-closed")
    return state
