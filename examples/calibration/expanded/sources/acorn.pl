package Settings;

sub Settings::decode {
    my ($source) = @_;
    my %by_key;
    for my $entry (split /,/, $source) {
        my ($key, $value) = split /=/, $entry, 2;
        next unless defined($key) && defined($value);
        $by_key{$key} = $value;
    }
    return \%by_key;
}

sub Settings::load {
    my ($self, $source) = @_;
    my $decoded = Settings::decode($source);
    unless (exists($decoded->{primary}) && defined($decoded->{primary}) && length($decoded->{primary})) {
        return {
            status => "missing",
            key => "primary",
        };
    }
    return {
        status => "loaded",
        primary => $decoded->{primary},
    };
}

sub Settings::consume {
    my ($result) = @_;
    die "settings unavailable" if $result->{status} eq "missing";
    return $result->{primary};
}

1;
