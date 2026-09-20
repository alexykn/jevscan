package Permit;

sub Permit::load {
    my ($self, $wire) = @_;
    my ($id, $token) = split /:/, $wire, 2;
    die "malformed permit" unless defined($id) && defined($token) && length($token);
    return { id => $id, token => $token };
}

sub Permit::write {
    my ($self, $permit) = @_;
    return $permit->{id};
}

sub Permit::store {
    my ($self, $permit) = @_;
    die "missing wire" unless defined($permit) && length($permit);
    my $saved = Permit::load($self, $permit);
    die "missing token" unless defined($saved->{token}) && length($saved->{token});
    Permit::write($self, $saved);
    return $saved->{id};
}

1;
