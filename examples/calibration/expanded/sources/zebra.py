def open_reader(path, opener):
    handle = opener.open(path, "rb")
    try:
        return decode(handle.read())
    finally:
        handle.close()


def open_writer(path, opener, value):
    handle = opener.open(path, "wb")
    try:
        handle.write(encode(value))
        return value
    finally:
        handle.close()


def open_second_reader(path, opener):
    handle = opener.open(path, "rb")
    try:
        return decode(handle.read())
    finally:
        handle.close()
