class Archive:
    def read(self, stream):
        entries = self._read_entries(stream)
        self._record_offsets(entries)
        return entries

    def _read_entries(self, stream):
        return decode_entries(stream)

    def _record_offsets(self, entries):
        self.offsets = [entry["offset"] for entry in entries]
