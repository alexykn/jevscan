import csv


def stream_rows(handle, sink):
    for row in csv.DictReader(handle):
        sink.write(row)
        yield row
    sink.flush()
