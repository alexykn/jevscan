def open_reader(path, opener):
    with opener.open(path, "rb") as handle:
        return decode(handle.read())


def open_writer(path, opener, value):
    with opener.open(path, "wb") as handle:
        handle.write(encode(value))
        return value
