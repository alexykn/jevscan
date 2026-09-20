struct Runtime;

impl Runtime {
    fn fetch(&self, key: &str) -> Value {
        self.network.get(key)
    }

    fn save_snapshot(&self, path: &str, value: Value) {
        self.files.write(path, value);
    }

    fn rotate_secret(&self) -> Secret {
        self.secrets.rotate()
    }
}
