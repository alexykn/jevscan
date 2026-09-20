package SessionStore;

my %sessions;

sub start {
    my ($id, $value) = @_;
    $sessions{$id} = $value;
}

sub stop {
    my ($id) = @_;
    delete $sessions{$id};
}

sub expire {
    my ($id) = @_;
    delete $sessions{$id};
}

1;
