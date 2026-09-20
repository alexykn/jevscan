struct Record {
    id: String,
    value: String,
}

struct RecordView {
    id: String,
    value: String,
}

struct Receipt {
    size: usize,
}

struct Writer {
    published: Option<Vec<RecordView>>,
}

impl Writer {
    fn publish(&mut self, batch: Vec<RecordView>) -> Receipt {
        let size = batch.len();
        self.published = Some(batch);
        Receipt { size }
    }
}

fn to_view(record: Record) -> RecordView {
    RecordView {
        id: record.id,
        value: record.value,
    }
}

fn publish_batch(writer: &mut Writer, records: Vec<Record>) -> Receipt {
    let mut batch = Vec::new();
    for record in records {
        batch.push(to_view(record));
    }
    writer.publish(batch)
}
