def accept_value(value):
    if not isinstance(value, int):
        raise TypeError("value")
    if value < 0:
        raise ValueError("range")
    if not isinstance(value, int):
        raise TypeError("value")
    return value
