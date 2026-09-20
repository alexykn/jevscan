package Permit;

sub Permit::new {
    my ($class, $id, $token) = @_;
    die "missing token" unless defined($token) && length($token);
    my $read = sub {
        return ($id, $token);
    };
    return bless $read, $class;
}

sub Permit::write {
    my ($self, $permit) = @_;
    my ($id, $token) = $permit->();
    return $id;
}

sub Permit::store {
    my ($self, $permit) = @_;
    die "wrong permit type" unless ref($permit) eq __PACKAGE__;
    my ($id, $token) = $permit->();
    die "missing token" unless defined($token) && length($token);
    Permit::write($self, $permit);
    die "missing token" unless defined($token) && length($token);
    return $id;
}

1;
