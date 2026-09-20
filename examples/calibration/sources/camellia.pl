sub normalize_address {
    my ($value) = @_;
    return {
        street => lc $value->{street},
        city => lc $value->{city},
        postal_code => $value->{postal_code},
    };
}

sub ship_address {
    my ($value) = @_;
    return normalize_address($value);
}
