use std::fmt::Debug;

pub fn standalone(value: i32) -> i32 { value + 1 }
pub struct Service { value: i32 }
pub enum State { Ready, Finished }
pub trait Store {
    fn load(&self) -> i32;
    fn ready(&self) -> bool { true }
}
impl Service {
    pub fn new(value: i32) -> Self { Self { value } }
    pub async fn fetch(&self) -> i32 { self.value }
}
impl Store for Service {
    fn load(&self) -> i32 { self.value }
}
mod nested {
    pub fn helper() -> bool { true }
}
