pub struct Settings {
    endpoint: String,
}

impl Settings {
    pub fn from_text(text: &str) -> Self {
        let endpoint = text.trim().to_string();
        Self { endpoint }
    }

    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }
}
