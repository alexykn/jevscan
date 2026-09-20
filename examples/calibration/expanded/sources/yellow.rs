fn read_labels(source: &mut Source) -> Result<Vec<String>, ReadError> {
    let text = source.read()?;
    let labels: Vec<String> = text.lines().map(str::to_owned).collect();
    if labels.is_empty() {
        return Err(ReadError::Empty);
    }
    Ok(labels)
}
