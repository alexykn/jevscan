class Service:
    def __init__(self, opener, cache, metrics):
        self.opener = opener
        self.cache = cache
        self.metrics = metrics

    def __enter__(self):
        self.handle = self.opener.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.handle.close()

    def load(self, key):
        value = self.handle.read(key)
        self.cache[key] = value
        self.metrics.count("load")
        return value
