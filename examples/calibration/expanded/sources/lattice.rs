struct Window;

impl Window {
    fn open(&mut self, spec: &Spec) -> Handle {
        let handle = self.allocate(spec);
        self.configure(handle, spec);
        handle
    }

    fn allocate(&mut self, spec: &Spec) -> Handle {
        Handle::new(spec.size)
    }

    fn configure(&mut self, handle: Handle, spec: &Spec) {
        handle.set_mode(spec.mode);
    }
}
