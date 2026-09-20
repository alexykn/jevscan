enum Error {
    Failed,
}

struct Entry;

struct Store {
    present: bool,
    fail_write: bool,
    fail_sync: bool,
}

impl Store {
    fn remove(&mut self, _: &str) -> Result<(), Error> {
        self.present = false;
        Ok(())
    }

    fn write(&mut self, _: &Entry) -> Result<(), Error> {
        if self.fail_write {
            return Err(Error::Failed);
        }
        self.present = true;
        Ok(())
    }

    fn sync(&self) -> Result<(), Error> {
        if self.fail_sync {
            return Err(Error::Failed);
        }
        Ok(())
    }
}

fn replace_entry(store: &mut Store, old_key: &str, next: &Entry) -> Result<(), Error> {
    store.remove(old_key)?;
    store.write(next)?;
    store.sync()?;
    Ok(())
}
