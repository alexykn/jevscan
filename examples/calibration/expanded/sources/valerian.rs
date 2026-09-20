fn encode_record(value: &Record) -> String {
    format!("{}:{}", value.name.trim(), value.count)
}

fn encode_copy(value: &Record) -> String {
    format!("{}:{}", value.name.trim(), value.count)
}

fn path_for(value: &Record) -> String {
    encode_record(value)
}
