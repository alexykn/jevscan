class Ledger:
    def __init__(self, fail_index=False, fail_history=False):
        self.current = None
        self.index = {}
        self.history = []
        self.cursor = 0
        self.fail_index = fail_index
        self.fail_history = fail_history

    def write_current(self, key, value):
        self.current = (key, value)

    def write_index(self, key):
        if self.fail_index:
            raise OSError("index")
        self.index[key] = len(self.history)

    def write_history(self, key):
        if self.fail_history:
            raise OSError("history")
        self.history.append(key)

    def advance_cursor(self):
        self.cursor += 1


def advance_record(state, key, value):
    state.write_current(key, value)
    state.write_index(key)
    state.write_history(key)
    state.advance_cursor()
    return state.cursor
