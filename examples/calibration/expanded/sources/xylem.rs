fn read_labels(source: &mut Source) -> Vec<String> {
    let labels = source
        .read()
        .map(|text| text.lines().map(str::to_owned).collect::<Vec<_>>())
        .unwrap_or_default();
    if labels.is_empty() {
        return Vec::new();
    }
    labels
}
