class RecordOwner:
    def __init__(self):
        self.records = {}
        self.open_count = 0

    def start(self, key, value):
        self.records[key] = {"value": value, "state": "open"}
        self.open_count += 1

    def stop(self, key):
        record = self.records.pop(key)
        self.open_count -= 1
        return {**record, "state": "closed"}


class RecordReaper:
    def __init__(self):
        self.records = {}
        self.open_count = 0

    def adopt(self, key, value):
        self.records[key] = {"value": value, "state": "open"}
        self.open_count += 1

    def expire(self, key):
        record = self.records.pop(key)
        self.open_count -= 1
        return {**record, "state": "expired"}


class RecordService:
    def __init__(self):
        self.owner = RecordOwner()
        self.reaper = RecordReaper()

    def open(self, key, value):
        self.owner.start(key, value)
        self.reaper.adopt(key, value)

    def close(self, key):
        return self.owner.stop(key)

    def active_count(self):
        return self.reaper.open_count
