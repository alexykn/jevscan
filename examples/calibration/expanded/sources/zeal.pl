package Settings;

sub Settings::decode {
    my ($source) = @_;
    my @values = split /,/, $source;
    my %indexed;
    for my $index (0 .. $#values) {
        $indexed{$index} = $values[$index];
    }
    return \%indexed;
}

sub Settings::default {
    return {
        status => "ready",
        source => "default",
        primary => undef,
    };
}

sub Settings::load {
    my ($self, $source) = @_;
    my $settings;
    eval {
        my $decoded = Settings::decode($source);
        my $primary = $decoded->{primary};
        die "missing primary setting" unless defined($primary) && length($primary);
        $settings = {
            status => "ready",
            source => "file",
            primary => $primary,
        };
    };
    if ($@) {
        return Settings::default();
    }
    return $settings;
}

sub Settings::consume {
    my ($settings) = @_;
    die "primary setting required"
        unless defined($settings->{primary}) && length($settings->{primary});
    return $settings->{primary};
}

1;
