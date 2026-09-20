class RecordOwner:
    def __init__(self):
        self.records = {}

    def start(self, key, value):
        self.records[key] = {"value": value, "state": "open"}

    def stop(self, key):
        self.records.pop(key, None)

    def remove(self, key):
        self.records.pop(key, None)
