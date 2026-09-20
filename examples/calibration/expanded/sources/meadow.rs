fn save_record(record: &mut Record, store: &mut Store) -> bool {
    if !record.is_ready() {
        return false;
    }
    record.refresh();
    if !record.is_ready() {
        return false;
    }
    store.write(record);
    true
}
