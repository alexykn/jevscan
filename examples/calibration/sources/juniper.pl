package Permit;

sub save {
    my ($self, $token, $store) = @_;
    die "missing token" unless defined $token && length $token;
    my $permit = $store->build($token);
    die "missing token" unless defined $token && length $token;
    return $store->write($permit);
}

1;
