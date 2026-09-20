class Echo:
    def send(self, value, done):
        state = {"value": value, "done": done}
        return self._schedule(lambda: self._deliver(state))

    def _schedule(self, callback):
        return self._invoke(callback)

    def _invoke(self, callback):
        return callback()

    def _deliver(self, state):
        result = self.channel.send(state["value"])
        return state["done"](result)
