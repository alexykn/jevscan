struct Settings {
    values: Map,
}

impl Settings {
    fn from_text(text: &str) -> Self {
        Self {
            values: parse_settings(text),
        }
    }

    fn get(&self, key: &str) -> Option<&Value> {
        self.values.get(key)
    }

    fn contains(&self, key: &str) -> bool {
        self.values.contains_key(key)
    }
}
