class Store:
    def __init__(self):
        self.entries = []

    def write(self, entry):
        self.entries.append(entry)


class Audit:
    def __init__(self, fail=False):
        self.records = []
        self.fail = fail

    def record(self, entry):
        if self.fail:
            raise OSError("audit")
        self.records.append(entry)


def publish(store, audit, entry):
    store.write(entry)
    try:
        audit.record(entry)
    except OSError:
        return entry
    return entry
