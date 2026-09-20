def seal_value(value, make_record):
    limit = value

    def build():
        return make_record(limit)

    return build()
