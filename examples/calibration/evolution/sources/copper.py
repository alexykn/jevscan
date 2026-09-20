class Store:
    def __init__(self, fail_history=False, fail_sync=False):
        self.values = {}
        self.history = []
        self.fail_history = fail_history
        self.fail_sync = fail_sync

    def write_value(self, key, value):
        self.values[key] = value

    def write_history(self, key):
        if self.fail_history:
            raise OSError("history")
        self.history.append(key)

    def sync(self):
        if self.fail_sync:
            raise OSError("sync")


def apply_change(store, entry):
    store.write_value(entry["key"], entry["value"])
    store.write_history(entry["key"])
    store.sync()
