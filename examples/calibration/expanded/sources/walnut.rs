fn encode_json(value: &Record) -> String {
    serde_json::to_string(value).unwrap()
}

fn encode_binary(value: &Record) -> Vec<u8> {
    value.to_bytes()
}

fn path_for(value: &Record) -> String {
    encode_json(value)
}
