def stream_rows(handle, sink):
    header = handle.readline().rstrip("\n")
    columns = header.split(",")
    for line in handle:
        values = line.rstrip("\n").split(",")
        row = {}
        for index, column in enumerate(columns):
            row[column] = values[index].strip()
        sink.write(row)
        yield row
    sink.flush()
