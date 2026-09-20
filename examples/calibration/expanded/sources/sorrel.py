def validate_row(row):
    return isinstance(row["name"], str) and row["name"].strip()


def serialize_row(row):
    return f"{row['name']}:{row['count']}"
