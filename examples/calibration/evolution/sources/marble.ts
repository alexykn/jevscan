type InputRecord = { id: string; value: string };
type RecordView = { id: string; value: string };
type Receipt = { size: number };

class Writer {
  published: RecordView[] | null = null;

  publish(batch: RecordView[]): Receipt {
    this.published = batch.slice();
    return { size: batch.length };
  }
}

function toView(record: InputRecord): RecordView {
  return { id: record.id, value: record.value };
}

export function publishBatch(writer: Writer, records: InputRecord[]): Receipt {
  const batch: RecordView[] = [];
  for (const record of records) {
    batch.push(toView(record));
  }
  return writer.publish(batch);
}
