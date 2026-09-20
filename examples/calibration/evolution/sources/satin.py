def read_value(source):
    try:
        value = source.read()
        if value < 0:
            raise ValueError
        return value
    except (OSError, ValueError):
        return 0
