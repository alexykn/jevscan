package Notes;

sub add {
    my ($self, $note) = @_;
    validate_note($note);
    return write_note($self, $note);
}

sub validate_note {
    my ($note) = @_;
    die "missing body" unless length $note->{body};
}

sub write_note {
    my ($self, $note) = @_;
    return $self->{store}->insert($note);
}

1;
