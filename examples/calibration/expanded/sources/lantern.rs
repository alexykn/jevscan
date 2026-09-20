fn save_record(record: &Record, store: &mut Store) -> bool {
    if !record.is_ready() {
        return false;
    }
    if !record.is_ready() {
        return false;
    }
    store.write(record);
    true
}
