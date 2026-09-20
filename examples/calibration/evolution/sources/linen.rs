enum Error {
    Failed,
}

struct Entry {
    key: String,
}

struct Store {
    primary: bool,
    index: Vec<String>,
    history: Vec<String>,
    fail_history: bool,
}

impl Store {
    fn commit_primary(&mut self, _: &Entry) -> Result<(), Error> {
        self.primary = true;
        Ok(())
    }

    fn commit_index(&mut self, key: &str) -> Result<(), Error> {
        self.index.push(key.to_owned());
        Ok(())
    }

    fn commit_history(&mut self, key: &str) -> Result<(), Error> {
        if self.fail_history {
            return Err(Error::Failed);
        }
        self.history.push(key.to_owned());
        Ok(())
    }
}

fn finish_entry(store: &mut Store, entry: &Entry) -> Result<(), Error> {
    store.commit_primary(entry)?;
    store.commit_index(&entry.key)?;
    store.commit_history(&entry.key)?;
    Ok(())
}
