def make_filter(limit, select):
    captured_limit = limit

    def accept(value):
        return value <= captured_limit and select(value)

    return accept
