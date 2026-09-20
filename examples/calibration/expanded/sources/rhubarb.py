def validate_row(row):
    return (
        isinstance(row["name"], str)
        and row["name"].strip()
        and isinstance(row["count"], int)
        and row["count"] >= 0
    )


def check_row(row):
    return (
        isinstance(row["name"], str)
        and row["name"].strip()
        and isinstance(row["count"], int)
        and row["count"] >= 0
    )
