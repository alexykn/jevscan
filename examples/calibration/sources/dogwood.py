def load_name(record):
    return record["name"].strip()


def save_name(record, value):
    record["name"] = value.strip()
