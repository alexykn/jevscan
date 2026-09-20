export class Saver {
  save(records: Record[], sink: Sink) {
    const total = this._count(records);
    let index = this._firstIndex();
    while (index < total) {
      this._writeOne(records, index, sink);
      index = this._next(index);
    }
    return this._finish(sink);
  }

  _count(records: Record[]) {
    return records.length;
  }

  _firstIndex() {
    return 0;
  }

  _writeOne(records: Record[], index: number, sink: Sink) {
    sink.write(records[index]);
  }

  _next(index: number) {
    return index + 1;
  }

  _finish(sink: Sink) {
    return sink.finish();
  }
}
