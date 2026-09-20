fn move_entry(resource: &mut Resource, old_key: Key, next: Entry) -> Result<(), Error> {
    resource.remove(old_key)?;
    resource.insert(next)?;
    Ok(())
}
