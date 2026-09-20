package Marker;

sub Marker::new {
    my ($class, %options) = @_;
    return bless {
        current => undef,
        history => [],
        cursor => 0,
        fail_history => $options{fail_history},
        fail_cursor => $options{fail_cursor},
    }, $class;
}

sub Marker::write_current {
    my ($self, $key, $value) = @_;
    $self->{current} = { key => $key, value => $value };
}

sub Marker::write_history {
    my ($self, $key) = @_;
    die "history" if $self->{fail_history};
    push @{$self->{history}}, $key;
}

sub Marker::advance_cursor {
    my ($self) = @_;
    die "cursor" if $self->{fail_cursor};
    $self->{cursor}++;
}

sub Marker::cursor {
    my ($self) = @_;
    return $self->{cursor};
}

sub Marker::apply {
    my ($state, $key, $value) = @_;
    $state->write_current($key, $value);
    $state->write_history($key);
    $state->advance_cursor();
    return $state->cursor();
}

1;
