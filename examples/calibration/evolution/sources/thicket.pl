package Mark;

sub Mark::set_first {
    my ($state, $value) = @_;
    $state->{value} = $value;
    return $state->{value};
}

sub Mark::set_second {
    my ($state, $value) = @_;
    $state->{value} = $value;
    return $state->{value};
}

1;
